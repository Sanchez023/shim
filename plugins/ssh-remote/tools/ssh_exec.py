"""SSH command execution with ControlMaster connection reuse."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

_MAX_OUTPUT_CHARS = 20_000
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
_WSL_CONTROL_PATH = "/tmp/oh-ssh-%C"


class SshExecInput(BaseModel):
    """Arguments for a remote SSH command."""

    host: str = Field(description="Hostname or IP address (no leading dash)")
    command: str = Field(description="Remote command to execute")
    user: str | None = Field(default=None, description="Remote user; omit to let ssh choose")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    identity_file: str | None = Field(
        default=None,
        description=(
            "Private key path as seen by the SSH implementation. On Windows this is a "
            "WSL path such as /home/<user>/.ssh/id_ed25519; omit to use default keys."
        ),
    )
    persist_seconds: int = Field(
        default=600,
        ge=0,
        le=86_400,
        description="How long an idle ControlMaster stays alive (0 disables persistence)",
    )
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    accept_new_host_key: bool = Field(
        default=True,
        description="Add an unknown host key to known_hosts on first connect",
    )


def _validate_token(value: str, name: str) -> str:
    if not value or not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def _use_wsl() -> bool:
    return sys.platform == "win32" and shutil.which("wsl.exe") is not None


def build_ssh_command(
    *,
    host: str,
    command: str,
    user: str | None = None,
    port: int = 22,
    identity_file: str | None = None,
    persist_seconds: int = 600,
    accept_new_host_key: bool = True,
    use_wsl: bool | None = None,
) -> list[str]:
    """Build the ssh argv, routing through WSL on Windows when available."""
    host = _validate_token(host, "host")
    if user is not None:
        user = _validate_token(user, "user")
    wsl = _use_wsl() if use_wsl is None else use_wsl

    control_path = _WSL_CONTROL_PATH if wsl else str(Path.home() / ".ssh" / "cm-%C")
    ssh_args = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path}",
        "-o",
        f"ControlPersist={int(persist_seconds)}",
    ]
    if accept_new_host_key:
        ssh_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    if port != 22:
        ssh_args.extend(["-p", str(port)])
    if identity_file:
        ssh_args.extend(["-i", identity_file])
    target = f"{user}@{host}" if user else host
    ssh_args.extend([target, command])
    return ["wsl.exe", "-e", *ssh_args] if wsl else ssh_args


class SshExecTool(BaseTool):
    """Run a command over SSH while reusing one ControlMaster per host."""

    name = "ssh_exec"
    description = (
        "Run a command on a remote host over SSH. Connection reuse is enabled with "
        "OpenSSH ControlMaster/ControlPersist. On Windows it automatically uses WSL's "
        "OpenSSH because native Windows ssh does not support multiplexing. Requires "
        "key-based authentication (BatchMode)."
    )
    input_model = SshExecInput

    def is_read_only(self, arguments: SshExecInput) -> bool:
        del arguments
        return False

    async def execute(
        self,
        arguments: SshExecInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        try:
            argv = build_ssh_command(
                host=arguments.host,
                command=arguments.command,
                user=arguments.user,
                port=arguments.port,
                identity_file=arguments.identity_file,
                persist_seconds=arguments.persist_seconds,
                accept_new_host_key=arguments.accept_new_host_key,
            )
        except ValueError as exc:
            return ToolResult(output=f"ssh_exec rejected input: {exc}", is_error=True)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except OSError as exc:
            return ToolResult(output=f"ssh_exec failed to start ssh: {exc}", is_error=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(
                output=f"ssh_exec timed out after {arguments.timeout_seconds} seconds",
                is_error=True,
            )

        body_parts = []
        if stdout:
            body_parts.append(stdout.decode("utf-8", errors="replace").rstrip())
        if stderr:
            body_parts.append(stderr.decode("utf-8", errors="replace").rstrip())
        output = "\n".join(part for part in body_parts if part).strip() or "(no output)"
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
        transport = "wsl" if argv[0] == "wsl.exe" else "native"
        return ToolResult(
            output=output,
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode, "transport": transport},
        )
