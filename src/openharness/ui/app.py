"""Interactive session entry points."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, TextIO

from openharness.api.client import SupportsStreamingMessages
from openharness.coordinator.coordinator_mode import is_coordinator_mode
from openharness.domain.stream_events import StreamEvent
from openharness.ui.backend_host import run_backend_host
from openharness.ui.coordinator_drain import drain_coordinator_async_agents
from openharness.ui.react_launcher import launch_react_tui
from openharness.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime
from openharness.ui.thinking import extract_thinking_text


def _decode_task_worker_line(raw: str) -> str:
    """Normalize one stdin line for the headless task worker.

    Task-manager driven agent workers receive either:
    - a plain text line (initial prompt or simple follow-up), or
    - a JSON object from ``send_message`` / teammate backends with a ``text`` field.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
    return stripped


async def run_repl(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    backend_only: bool = False,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    permission_mode: str | None = None,
    resume_session_id: str | None = None,
    continue_session: bool = False,
    open_session_picker: bool = False,
) -> None:
    """Run the default OpenHarness interactive application (React TUI)."""
    if backend_only:
        await run_backend_host(
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            effort=effort,
            personality=personality,
            tool_profile=tool_profile,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            api_client=api_client,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=max_turns is not None,
            permission_mode=permission_mode,
        )
        return

    exit_code = await launch_react_tui(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        personality=personality,
        tool_profile=tool_profile,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        permission_mode=permission_mode,
        resume_session_id=resume_session_id,
        continue_session=continue_session,
        open_session_picker=open_session_picker,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


_MAX_TOOL_ARG_CHARS = 80
_MAX_TOOL_LINE_CHARS = 240


def _summarize_tool_value(value: Any) -> str:
    """Compact one tool argument into a single log-friendly string."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    if len(text) > _MAX_TOOL_ARG_CHARS:
        text = text[: _MAX_TOOL_ARG_CHARS - 1] + "…"
    return text


def _format_tool_arguments(arguments: dict[str, Any] | None) -> str:
    """Return a one-line rendering of a tool call's arguments."""
    if not arguments:
        return ""
    parts = [
        f"{key}={_summarize_tool_value(value)}"
        for key, value in arguments.items()
        if value not in (None, "")
    ]
    line = ", ".join(parts)
    if len(line) > _MAX_TOOL_LINE_CHARS:
        line = line[: _MAX_TOOL_LINE_CHARS - 1] + "…"
    return line


def _first_line(text: str, limit: int = 200) -> str:
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line


class _TaskWorkerLog:
    """Render engine events into the plain-text log read by ``task_output``.

    Agent workers previously wrote only assistant text plus one newline per
    turn, so a running agent's log was a wall of blank lines with no record of
    which tools it was running. Tool starts and failures are now logged as
    compact ``[tool N]`` lines, and empty turns no longer emit blank lines.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._text_since_turn = False
        self._ends_with_newline = True
        self._tool_count = 0
        self._thinking_open = False
        self._thinking_streamed = False

    def write(self, text: str) -> None:
        if not text:
            return
        self._stream.write(text)
        self._stream.flush()
        self._ends_with_newline = text.endswith("\n")

    def line(self, message: str) -> None:
        if not self._ends_with_newline:
            self.write("\n")
        self.write(message + "\n")

    def _close_thinking(self) -> None:
        if self._thinking_open:
            if not self._ends_with_newline:
                self.write("\n")
            self._thinking_open = False

    async def render(self, event: StreamEvent) -> None:
        from openharness.domain.stream_events import (
            AssistantTextDelta,
            AssistantThinkingDelta,
            AssistantTurnComplete,
            ErrorEvent,
            StatusEvent,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )

        if not isinstance(event, AssistantThinkingDelta):
            self._close_thinking()

        if isinstance(event, AssistantTextDelta):
            if event.text:
                self.write(event.text)
                self._text_since_turn = True
        elif isinstance(event, AssistantThinkingDelta):
            if event.text:
                if not self._thinking_open:
                    if not self._ends_with_newline:
                        self.write("\n")
                    self.write("[thinking] ")
                    self._thinking_open = True
                self.write(event.text)
                self._thinking_streamed = True
        elif isinstance(event, AssistantTurnComplete):
            if not self._thinking_streamed:
                thinking = extract_thinking_text(event.message)
                if thinking:
                    self.line(f"[thinking] {thinking}")
            self._thinking_streamed = False
            if self._text_since_turn:
                if not self._ends_with_newline:
                    self.write("\n")
            else:
                # Non-streaming backends may only populate the completed
                # message. Do not lose the turn text when no deltas arrived.
                message_text = getattr(event.message, "text", "") or ""
                if message_text.strip():
                    self.write(message_text)
                    if not message_text.endswith("\n"):
                        self.write("\n")
            self._text_since_turn = False
            self._ends_with_newline = True
        elif isinstance(event, ToolExecutionStarted):
            self._tool_count += 1
            arguments = _format_tool_arguments(event.tool_input)
            suffix = f"({arguments})" if arguments else ""
            self.line(f"[tool {self._tool_count}] {event.tool_name}{suffix}")
        elif isinstance(event, ToolExecutionCompleted):
            if event.is_error:
                detail = _first_line(event.output) or "tool failed"
                self.line(f"[tool error] {event.tool_name}: {detail}")
        elif isinstance(event, ErrorEvent):
            self.line(f"[error] {event.message}")
        elif isinstance(event, StatusEvent) and event.message:
            self.line(f"[status] {event.message}")


async def run_task_worker(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
) -> None:
    """Run a stdin-driven headless worker for background agent tasks.

    This mode exists for subprocess teammates and other task-manager managed
    agent processes. It intentionally avoids the React TUI / Ink path so it
    can run without a controlling TTY.
    """

    async def _noop_permission(_tool_name: str, _reason: str) -> bool:
        return True

    async def _noop_ask(_question: str, _options: list[str] | None = None) -> str:
        return ""

    worker_log = _TaskWorkerLog()

    async def _print_system(message: str) -> None:
        worker_log.line(message)

    async def _render_event(event: StreamEvent) -> None:
        await worker_log.render(event)

    async def _clear_output() -> None:
        return None

    bundle = await build_runtime(
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        personality=personality,
        tool_profile=tool_profile,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        api_client=api_client,
        permission_prompt=_noop_permission,
        ask_user_prompt=_noop_ask,
        enforce_max_turns=max_turns is not None,
        permission_mode=permission_mode,
    )
    await start_runtime(bundle)
    try:
        while True:
            raw = await asyncio.to_thread(sys.stdin.readline)
            if raw == "":
                break
            line = _decode_task_worker_line(raw)
            if not line:
                continue
            await handle_line(
                bundle,
                line,
                print_system=_print_system,
                render_event=_render_event,
                clear_output=_clear_output,
            )
            # Background agent tasks are one-shot workers. If the coordinator
            # needs to send a follow-up later, BackgroundTaskManager already
            # knows how to restart the task and write the next stdin payload.
            break
    finally:
        await close_runtime(bundle)


async def run_print_mode(
    *,
    prompt: str,
    output_format: str = "text",
    cwd: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
    max_turns: int | None = None,
) -> None:
    """Non-interactive mode: submit prompt, stream output, exit."""
    from openharness.domain.stream_events import (
        AssistantTextDelta,
        AssistantThinkingDelta,
        AssistantTurnComplete,
        CompactProgressEvent,
        ErrorEvent,
        StatusEvent,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

    async def _noop_permission(tool_name: str, reason: str) -> bool:
        return True

    async def _noop_ask(question: str) -> str:
        return ""

    bundle = await build_runtime(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        personality=personality,
        tool_profile=tool_profile,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        enforce_max_turns=True,
        api_client=api_client,
        permission_prompt=_noop_permission,
        ask_user_prompt=_noop_ask,
    )
    await start_runtime(bundle)

    collected_text = ""
    events_list: list[dict] = []

    try:
        async def _print_system(message: str) -> None:
            nonlocal collected_text
            if output_format == "text":
                print(message, file=sys.stderr)
            elif output_format == "stream-json":
                obj = {"type": "system", "message": message}
                print(json.dumps(obj), flush=True)
                events_list.append(obj)

        async def _render_event(event: StreamEvent) -> None:
            nonlocal collected_text
            if isinstance(event, AssistantTextDelta):
                collected_text += event.text
                if output_format == "text":
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif output_format == "stream-json":
                    obj = {"type": "assistant_delta", "text": event.text}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, AssistantThinkingDelta):
                if output_format == "stream-json":
                    obj = {"type": "thinking_delta", "text": event.text}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, AssistantTurnComplete):
                if output_format == "text":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif output_format == "stream-json":
                    obj = {"type": "assistant_complete", "text": event.message.text.strip()}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionStarted):
                if output_format == "stream-json":
                    obj = {"type": "tool_started", "tool_name": event.tool_name, "tool_input": event.tool_input}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionCompleted):
                if output_format == "stream-json":
                    obj = {"type": "tool_completed", "tool_name": event.tool_name, "output": event.output, "is_error": event.is_error}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ErrorEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "error", "message": event.message, "recoverable": event.recoverable}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, CompactProgressEvent):
                if output_format == "text" and event.message:
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {
                        "type": "compact_progress",
                        "phase": event.phase,
                        "trigger": event.trigger,
                        "attempt": event.attempt,
                        "message": event.message,
                    }
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, StatusEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "status", "message": event.message}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)

        async def _clear_output() -> None:
            pass

        await handle_line(
            bundle,
            prompt,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )
        if is_coordinator_mode():
            await drain_coordinator_async_agents(
                bundle,
                prompt_seed=prompt,
                print_system=_print_system,
                render_event=_render_event,
                announce_waiting=output_format == "text",
            )

        if output_format == "json":
            result = {"type": "result", "text": collected_text.strip()}
            print(json.dumps(result))
    finally:
        await close_runtime(bundle)
