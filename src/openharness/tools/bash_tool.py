"""Shell command execution tool."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from openharness.permissions.command_safety import classify_command, is_read_only_command
from openharness.sandbox import SandboxUnavailableError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.shell import create_shell_subprocess

_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS = 2.0


class BashToolInput(BaseModel):
    """Arguments for the bash tool."""

    command: str = Field(description="Shell command to execute")
    cwd: str | None = Field(default=None, description="Working directory override")
    timeout_seconds: int = Field(default=600, ge=1, le=3600)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _clamp_timeout(cls, value: object) -> object:
        """Clamp oversized timeouts instead of failing the tool call."""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return value
        return max(1, min(number, 3600))


class BashTool(BaseTool):
    """Execute a shell command with stdout/stderr capture."""

    name = "bash"
    description = (
        "Run a shell command in the local repository. Use && or ; for dependent "
        "steps in one call; issue independent commands as separate parallel bash calls."
    )
    input_model = BashToolInput

    def is_read_only(self, arguments: BaseModel) -> bool:
        """Return True for commands that only inspect the workspace.

        Default permission mode auto-allows read-only tools, so this keeps
        commands such as ``git status`` from prompting while every command the
        classifier does not positively recognise still asks for confirmation.
        """
        if not isinstance(arguments, BashToolInput):
            return False
        return is_read_only_command(arguments.command, cwd=arguments.cwd)

    def read_only_verdict(self, arguments: BaseModel) -> str | None:
        """Classify the command, keeping ``unknown`` distinct from ``mutating``.

        Plan mode refuses mutating commands outright but has to ask for the ones
        it merely cannot parse (``$(...)``, ``xargs``); collapsing both into
        ``False`` turned every such command into a dead end (ADR-0013 D2).
        """
        if not isinstance(arguments, BashToolInput):
            return None
        return classify_command(arguments.command, cwd=arguments.cwd).verdict.value

    def read_only_reason(self, arguments: BaseModel) -> str | None:
        """Why the classifier answered as it did (``write_flag``, ``xargs``, …)."""
        if not isinstance(arguments, BashToolInput):
            return None
        return classify_command(arguments.command, cwd=arguments.cwd).reason or None

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        cwd = Path(arguments.cwd).expanduser() if arguments.cwd else context.cwd
        preflight_error = _preflight_interactive_command(arguments.command)
        if preflight_error is not None:
            return ToolResult(
                output=preflight_error,
                is_error=True,
                metadata={"interactive_required": True},
            )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await create_shell_subprocess(
                arguments.command,
                cwd=cwd,
                prefer_pty=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process(process, force=False)
            raise

        # Drain stdout while the process runs.  Waiting for exit before reading
        # deadlocks as soon as the child fills the OS pipe buffer (~64 KiB on
        # Windows): the child blocks on write, so it never exits and wait()
        # never returns.  Large `git diff`/`git log` output used to hang the
        # tool for the full timeout because of this.
        output_buffer = bytearray()
        reader = (
            asyncio.create_task(_read_stream_into(process.stdout, output_buffer))
            if process.stdout is not None
            else None
        )

        try:
            detached = await asyncio.wait_for(
                _wait_with_background(process, context.background_event),
                timeout=arguments.timeout_seconds,
            )
        except TimeoutError:
            await _terminate_process(process, force=True)
            await _settle_stream_reader(reader)
            return ToolResult(
                output=_format_timeout_output(
                    output_buffer,
                    command=arguments.command,
                    timeout_seconds=arguments.timeout_seconds,
                ),
                is_error=True,
                metadata={"returncode": process.returncode, "timed_out": True},
            )
        except asyncio.CancelledError:
            await _terminate_process(process, force=False)
            await _settle_stream_reader(reader)
            raise

        if detached:
            return await _hand_off_to_background(
                process=process,
                reader=reader,
                output_buffer=output_buffer,
                command=arguments.command,
                cwd=cwd,
            )

        await _settle_stream_reader(reader)
        text = _format_output(output_buffer)
        return ToolResult(
            output=text,
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode},
        )


async def _terminate_process(process: asyncio.subprocess.Process, *, force: bool) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await _terminate_windows_process_tree(process, force=force)
        return
    if force:
        process.kill()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0 if force else 2.0)
    except TimeoutError:
        if force:
            return
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            # The process is already killed; never let teardown hang the
            # command's timeout path.
            return


async def _terminate_windows_process_tree(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    """Kill a Windows shell and its children with ``taskkill /T``.

    ``Process.terminate()`` only terminates the shell process; long-running
    grandchildren such as ``npm`` or ``pytest`` keep running and hold files and
    ports open. ``taskkill /T`` walks the process tree.
    """
    pid = getattr(process, "pid", None)
    if pid is not None:
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        try:
            killer = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=3.0)
        except (OSError, TimeoutError):
            pass
    if process.returncode is None:
        if force:
            process.kill()
        else:
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0 if force else 2.0)
    except TimeoutError:
        if not force:
            process.kill()


async def _wait_with_background(
    process: asyncio.subprocess.Process,
    background_event: asyncio.Event | None,
) -> bool:
    """Wait for *process*, returning True when the user asked to background it."""
    if background_event is None:
        await process.wait()
        return False
    wait_task = asyncio.create_task(process.wait())
    detach_task = asyncio.create_task(background_event.wait())
    try:
        done, _ = await asyncio.wait(
            {wait_task, detach_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (wait_task, detach_task):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return detach_task in done and wait_task not in done


async def _hand_off_to_background(
    *,
    process: asyncio.subprocess.Process,
    reader: asyncio.Task[None] | None,
    output_buffer: bytearray,
    command: str,
    cwd: Path,
) -> ToolResult:
    """Move a still-running command into the background task manager (Ctrl+B)."""
    from openharness.tasks.manager import get_task_manager

    # Stop draining before the task manager takes the pipe, otherwise two
    # readers split the output between them.
    if reader is not None and not reader.done():
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    record = await get_task_manager().adopt_shell_process(
        process=process,
        command=command,
        cwd=cwd,
        initial_output=bytes(output_buffer),
    )
    captured = _format_output(output_buffer)
    return ToolResult(
        output=(
            f"Command moved to the background as task {record.id}.\n"
            f"Output so far:\n{captured}\n\n"
            f"Use TaskOutput with task_id={record.id} to read more output "
            f"(or TaskStop to kill it)."
        ),
        metadata={"task_id": record.id, "backgrounded": True},
    )


async def _read_stream_into(
    stream: asyncio.StreamReader,
    output_buffer: bytearray,
) -> None:
    """Copy *stream* into *output_buffer* until EOF, draining as data arrives."""
    while True:
        try:
            chunk = await stream.read(65536)
        except OSError:
            return
        if not chunk:
            return
        output_buffer.extend(chunk)


async def _settle_stream_reader(reader: asyncio.Task[None] | None) -> None:
    """Give the drain task a bounded window to finish, then cancel it.

    A grandchild that inherited the pipe can keep it open after the shell is
    killed; waiting for EOF forever would reintroduce the hang this reader
    exists to prevent.
    """
    if reader is None or reader.done():
        return
    done, _ = await asyncio.wait({reader}, timeout=_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS)
    if not done:
        reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)


def _format_output(output_buffer: bytearray) -> str:
    text = output_buffer.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if not text:
        return "(no output)"
    if len(text) > 12000:
        return f"{text[:12000]}\n...[truncated]..."
    return text


def _format_timeout_output(output_buffer: bytearray, *, command: str, timeout_seconds: int) -> str:
    parts = [f"Command timed out after {timeout_seconds} seconds."]
    text = _format_output(output_buffer)
    if text != "(no output)":
        parts.extend(["", "Partial output:", text])
    hint = _interactive_command_hint(command=command, output=text)
    if hint:
        parts.extend(["", hint])
    return "\n".join(parts)


def _preflight_interactive_command(command: str) -> str | None:
    lowered_command = command.lower()
    if not _looks_like_interactive_scaffold(lowered_command):
        return None
    return (
        "This command appears to require interactive input before it can continue. "
        "The bash tool is non-interactive, so it cannot answer installer/scaffold prompts live. "
        "Prefer non-interactive flags (for example --yes, -y, --skip-install, --defaults, --non-interactive), "
        "or run the scaffolding step once in an external terminal before asking the agent to continue."
    )


def _interactive_command_hint(*, command: str, output: str) -> str | None:
    lowered_command = command.lower()
    if _looks_like_interactive_scaffold(lowered_command) or _looks_like_prompt(output):
        return (
            "This command appears to require interactive input. "
            "The bash tool is non-interactive, so prefer non-interactive flags "
            "(for example --yes, -y, --skip-install, or similar) or run the "
            "scaffolding step once in an external terminal before continuing."
        )
    return None


def _looks_like_interactive_scaffold(lowered_command: str) -> bool:
    scaffold_markers: tuple[str, ...] = (
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "pnpm dlx ",
        "npm init ",
        "pnpm init ",
        "yarn init ",
        "bunx create-",
        "npx create-",
    )
    non_interactive_markers: tuple[str, ...] = (
        "--yes",
        " -y",
        "--skip-install",
        "--defaults",
        "--non-interactive",
        "--ci",
    )
    return any(marker in lowered_command for marker in scaffold_markers) and not any(
        marker in lowered_command for marker in non_interactive_markers
    )


def _looks_like_prompt(output: str) -> bool:
    if not output:
        return False
    prompt_markers: Iterable[str] = (
        "would you like",
        "ok to proceed",
        "select an option",
        "which",
        "press enter to continue",
        "?",
    )
    lowered_output = output.lower()
    return any(marker in lowered_output for marker in prompt_markers)
