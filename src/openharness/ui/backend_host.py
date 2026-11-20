"""JSON-lines backend host for the React terminal frontend."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.api.client import SupportsStreamingMessages
from openharness.bridge import get_bridge_manager
from openharness.commands import MemoryCommandBackend
from openharness.coordinator.coordinator_mode import is_coordinator_mode
from openharness.domain.messages import ContentBlock, ConversationMessage, ImageBlock, TextBlock
from openharness.domain.stream_events import (
    AssistantTextDelta,
    AssistantThinkingDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ContextChangedEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.services.session_backend import SessionBackend
from openharness.tasks import get_task_manager
from openharness.ui.backend_turns import TurnLifecycleMixin
from openharness.ui.coordinator_drain import drain_coordinator_async_agents
from openharness.ui.protocol import (
    BackendEvent,
    FrontendImageAttachment,
    FrontendRequest,
    TranscriptItem,
)
from openharness.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime
from openharness.ui.select_commands import build_select_command_line, build_select_prompt
from openharness.ui.thinking import extract_thinking_text

log = logging.getLogger(__name__)

_PROTOCOL_PREFIX = "OHJSON:"


def _import_module_quietly(module_name: str) -> None:
    """Import *module_name*, swallowing failures (warmup is best effort)."""
    try:
        importlib.import_module(module_name)
    except Exception:
        log.debug("SDK warmup failed for %s", module_name, exc_info=True)


@dataclass(frozen=True)
class BackendHostConfig:
    """Configuration for one backend host session."""

    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None
    personality: str | None = None
    tool_profile: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    api_key: str | None = None
    api_format: str | None = None
    active_profile: str | None = None
    api_client: SupportsStreamingMessages | None = None
    cwd: str | None = None
    restore_messages: list[dict[str, Any]] | None = None
    restore_tool_metadata: dict[str, object] | None = None
    enforce_max_turns: bool = True
    permission_mode: str | None = None
    session_backend: SessionBackend | None = None
    extra_skill_dirs: tuple[str, ...] = ()
    extra_plugin_roots: tuple[str, ...] = ()
    memory_backend: MemoryCommandBackend | None = None
    include_project_memory: bool = True
    autodream_context: dict[str, object] | None = None


class ReactBackendHost(TurnLifecycleMixin):
    """Drive the OpenHarness runtime over a structured stdin/stdout protocol."""

    def __init__(self, config: BackendHostConfig) -> None:
        self._config = config
        self._bundle = None
        self._write_lock = asyncio.Lock()
        self._request_queue: asyncio.Queue[FrontendRequest] = asyncio.Queue()
        self._permission_requests: dict[str, asyncio.Future[bool]] = {}
        self._edit_approval_requests: dict[str, asyncio.Future[str]] = {}
        self._question_requests: dict[str, asyncio.Future[str]] = {}
        self._permission_lock = asyncio.Lock()
        self._busy = False
        self._running = True
        self._active_request_task: asyncio.Task[Any] | None = None
        # Track last tool input per name for rich event emission
        self._last_tool_inputs: dict[str, dict[str, Any]] = {}
        self._project_files: list[str] | None = None
        self._edit_always_approved = False
        self._thinking_streamed = False
        self._last_permission_mode = ""
        # Plan file the todo panel was last painted from.  Resuming a different
        # session swaps this path under the engine, and the panel has to follow.
        self._last_plan_file = ""
        # Checklist last pushed to the panel.  Commands such as ``/todos clear``
        # rewrite the plan file without moving it, so the path alone is not
        # enough to decide whether a repaint is needed.
        self._last_todo_markdown = ""

    async def run(self) -> int:
        self._bundle = await build_runtime(
            model=self._config.model,
            max_turns=self._config.max_turns,
            effort=self._config.effort,
            personality=self._config.personality,
            tool_profile=self._config.tool_profile,
            base_url=self._config.base_url,
            system_prompt=self._config.system_prompt,
            api_key=self._config.api_key,
            api_format=self._config.api_format,
            active_profile=self._config.active_profile,
            api_client=self._config.api_client,
            cwd=self._config.cwd,
            restore_messages=self._config.restore_messages,
            restore_tool_metadata=self._config.restore_tool_metadata,
            permission_prompt=self._ask_permission,
            ask_user_prompt=self._ask_question,
            edit_approval_prompt=self._ask_edit_approval,
            enforce_max_turns=self._config.enforce_max_turns,
            permission_mode=self._config.permission_mode,
            session_backend=self._config.session_backend,
            extra_skill_dirs=self._config.extra_skill_dirs,
            extra_plugin_roots=self._config.extra_plugin_roots,
            memory_backend=self._config.memory_backend,
            include_project_memory=self._config.include_project_memory,
            autodream_context=self._config.autodream_context,
        )
        await start_runtime(self._bundle)
        if self._bundle is not None:
            set_output_callback = getattr(
                self._bundle.engine, "set_tool_output_callback", None
            )
            if callable(set_output_callback):
                set_output_callback(self._emit_tool_output)
            self._last_permission_mode = str(self._bundle.app_state.get().permission_mode or "")
        command_list = self._bundle.commands.list_commands()
        await self._emit(
            BackendEvent.ready(
                self._bundle.app_state.get(),
                get_task_manager().list_tasks(),
                [f"/{command.name}" for command in command_list],
                {
                    f"/{command.name}": command.description
                    for command in command_list
                },
                [f"/{command.name}" for command in command_list if command.busy_safe],
            )
        )
        self._warm_sdk()
        await self._replay_restored_transcript()
        await self._emit(self._status_snapshot())
        bundle = self._bundle
        if bundle is not None:
            plan_file = str(bundle.engine.tool_metadata.get("plan_file") or "")
            self._last_plan_file = plan_file
            if plan_file:
                await self._emit_todo_file_update(plan_file)

        reader = asyncio.create_task(self._read_requests())
        try:
            while self._running:
                request = await self._request_queue.get()
                if request.type == "shutdown":
                    await self._emit(BackendEvent(type="shutdown"))
                    break
                if request.type == "interrupt":
                    await self._interrupt_active_request()
                    continue
                if request.type == "background_task":
                    await self._background_active_tool()
                    continue
                if request.type == "set_ui_preference":
                    await self._set_ui_preference(request.key or "", request.value or "")
                    continue
                if request.type in ("permission_response", "question_response"):
                    continue
                if request.type == "list_sessions":
                    await self._handle_list_sessions()
                    continue
                if request.type == "select_command":
                    await self._handle_select_command(request.command or "")
                    continue
                if request.type == "apply_select_command":
                    if self._busy:
                        await self._emit(BackendEvent(type="error", message="Session is busy"))
                        continue
                    self._busy = True
                    try:
                        should_continue = await self._run_active_request(
                            self._apply_select_command(
                                request.command or "",
                                request.value or "",
                            )
                        )
                    finally:
                        self._busy = False
                    if not should_continue:
                        await self._emit(BackendEvent(type="shutdown"))
                        break
                    continue
                if request.type == "search_files":
                    if self._bundle is None:
                        continue
                    from openharness.ui.file_search import list_project_files, rank_files

                    if self._project_files is None:
                        self._project_files = list_project_files(self._bundle.cwd)
                    await self._emit(
                        BackendEvent(
                            type="file_search_results",
                            files=rank_files(
                                self._project_files,
                                request.query or "",
                            ),
                        )
                    )
                    continue
                if request.type == "run_shell":
                    if self._busy:
                        await self._emit(
                            BackendEvent(type="error", message="Session is busy")
                        )
                        continue
                    self._busy = True
                    # Runs as its own task: a long `!command` must not freeze
                    # the request loop any more than a turn may.
                    self._start_detached(self._run_shell_command(request.line or ""))
                    continue
                if request.type != "submit_line":
                    await self._emit(BackendEvent(type="error", message=f"Unknown request type: {request.type}"))
                    continue
                if self._busy:
                    line = (request.line or "").strip()
                    if line and not request.images and self._is_busy_safe_line(line):
                        # Read-only commands (status, cost, diff, ...) run
                        # straight away instead of queueing behind the turn.
                        await self._run_busy_safe_line(line)
                        continue
                    if line and not request.images and not line.startswith("/"):
                        # A follow-up prompt: hand it to the engine so it lands
                        # in the conversation at the next step boundary.
                        await self._queue_message_during_turn(line)
                        continue
                    await self._emit(BackendEvent(type="error", message="Session is busy"))
                    continue
                line = (request.line or "").strip()
                if not line and not request.images:
                    continue
                self._busy = True
                self._start_turn(line, request.images)
                continue
        finally:
            active_turn = self._active_request_task
            if active_turn is not None and not active_turn.done():
                active_turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await active_turn
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            if self._bundle is not None:
                await close_runtime(self._bundle)
        return 0

    def _warm_sdk(self) -> None:
        """Import the active provider's SDK off the critical startup path.

        The provider clients import their vendor package lazily (see
        ``openharness.api.client._anthropic_sdk``) so that ``ready`` is not
        delayed by ~2s of ``anthropic``/``openai`` imports.  Warming the module
        in a daemon thread means the typing time before the first prompt pays
        for it, not the first answer.
        """
        bundle = self._bundle
        if bundle is None:
            return
        sdk_module = str(getattr(type(bundle.api_client), "sdk_module", "") or "")
        if not sdk_module:
            return
        threading.Thread(
            target=_import_module_quietly,
            args=(sdk_module,),
            name=f"sdk-warmup-{sdk_module}",
            daemon=True,
        ).start()

    async def _read_requests(self) -> None:
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                await self._request_queue.put(FrontendRequest(type="shutdown"))
                return
            payload = raw.decode("utf-8").strip()
            if not payload:
                continue
            try:
                request = FrontendRequest.model_validate_json(payload)
            except (ValueError, TypeError) as exc:  # pragma: no cover - defensive protocol handling
                await self._emit(BackendEvent(type="error", message=f"Invalid request: {exc}"))
                continue
            if request.type == "permission_response" and request.request_id in self._edit_approval_requests:
                edit_future = self._edit_approval_requests[request.request_id]
                if not edit_future.done():
                    edit_future.set_result(_edit_approval_reply_from_request(request))
                continue
            if request.type == "permission_response" and request.request_id in self._permission_requests:
                permission_future = self._permission_requests[request.request_id]
                if not permission_future.done():
                    permission_future.set_result(bool(request.allowed))
                continue
            if request.type == "question_response" and request.request_id in self._question_requests:
                question_future = self._question_requests[request.request_id]
                if not question_future.done():
                    question_future.set_result(request.answer or "")
                continue
            if request.type == "interrupt":
                await self._interrupt_active_request()
                continue
            await self._request_queue.put(request)

    async def _replay_restored_transcript(self) -> None:
        """Replay restored session messages so the TUI is not blank after a resume.

        The rows mirror a live transcript: tool calls, their collapsible
        results and per-block text, with harness-private context dropped.
        """
        from openharness.ui.transcript_replay import transcript_items_for_messages

        for item in transcript_items_for_messages(self._config.restore_messages or []):
            await self._emit(BackendEvent(type="transcript_item", item=item))

    async def _process_line(
        self,
        line: str,
        *,
        transcript_line: str | None = None,
        images: list[FrontendImageAttachment] | None = None,
        end_of_turn: bool = True,
        echo: bool = True,
    ) -> bool:
        assert self._bundle is not None
        from openharness.ui.file_mentions import expand_file_mentions

        model_line, attached_files = expand_file_mentions(line, self._bundle.cwd)
        user_message = _build_user_message_with_images(model_line, images or [])
        display_text = transcript_line or _format_transcript_line(line, images or [])
        if attached_files:
            display_text += (
                f"\n[{len(attached_files)} file(s) attached: "
                f"{', '.join(attached_files[:5])}"
                f"{', ...' if len(attached_files) > 5 else ''}]"
            )
        if echo:
            await self._emit(
                BackendEvent(
                    type="transcript_item",
                    item=TranscriptItem(
                        role="user",
                        text=display_text,
                    ),
                )
            )

        async def _print_system(message: str) -> None:
            await self._emit(
                BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=message))
            )

        async def _print_user(message: str) -> None:
            await self._emit(
                BackendEvent(type="transcript_item", item=TranscriptItem(role="user", text=message))
            )

        async def _render_item(item: TranscriptItem) -> None:
            await self._emit(BackendEvent(type="transcript_item", item=item))

        self._thinking_streamed = False

        async def _render_event(event: StreamEvent) -> None:
            if isinstance(event, AssistantTextDelta):
                await self._emit(BackendEvent(type="assistant_delta", message=event.text))
                return
            if isinstance(event, AssistantThinkingDelta):
                self._thinking_streamed = True
                await self._emit(BackendEvent(type="thinking_delta", message=event.text))
                return
            if isinstance(event, CompactProgressEvent):
                await self._emit(
                    BackendEvent(
                        type="compact_progress",
                        compact_phase=event.phase,
                        compact_trigger=event.trigger,
                        attempt=event.attempt,
                        compact_checkpoint=event.checkpoint,
                        compact_metadata=event.metadata,
                        message=event.message,
                    )
                )
                return
            if isinstance(event, AssistantTurnComplete):
                # Non-streaming providers only expose thinking at turn end.
                if not self._thinking_streamed:
                    thinking = extract_thinking_text(event.message)
                    if thinking:
                        await self._emit(
                            BackendEvent(type="thinking_delta", message=thinking + "\n\n")
                        )
                self._thinking_streamed = False
                await self._emit(
                    BackendEvent(
                        type="assistant_complete",
                        message=event.message.text.strip(),
                        item=TranscriptItem(role="assistant", text=event.message.text.strip()),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                # The response is part of the model-visible history now, and the
                # usage counters just moved: keep the ctx meter in step.
                await self._emit(self._status_snapshot())
                return
            if isinstance(event, ContextChangedEvent):
                # A user prompt or a batch of tool results was appended to the
                # history the next request will carry. Refresh immediately so
                # the status bar does not sit on a stale value for the whole
                # next model round-trip.
                await self._emit(self._status_snapshot())
                return
            if isinstance(event, ToolExecutionStarted):
                self._last_tool_inputs[event.tool_name] = event.tool_input or {}
                await self._emit(
                    BackendEvent(
                        type="tool_started",
                        tool_name=event.tool_name,
                        tool_use_id=event.tool_use_id or None,
                        tool_input=event.tool_input,
                        item=TranscriptItem(
                            role="tool",
                            text=f"{event.tool_name} {json.dumps(event.tool_input, ensure_ascii=True)}",
                            tool_name=event.tool_name,
                            tool_use_id=event.tool_use_id or None,
                            tool_input=event.tool_input,
                        ),
                    )
                )
                return
            if isinstance(event, ToolExecutionCompleted):
                await self._emit(
                    BackendEvent(
                        type="tool_completed",
                        tool_name=event.tool_name,
                        tool_use_id=event.tool_use_id or None,
                        output=event.output,
                        is_error=event.is_error,
                        item=TranscriptItem(
                            role="tool_result",
                            text=event.output,
                            tool_name=event.tool_name,
                            tool_use_id=event.tool_use_id or None,
                            is_error=event.is_error,
                            metadata=event.metadata,
                        ),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                await self._emit(self._status_snapshot())
                # Surface model-initiated mode switches before line_complete.
                bundle = self._bundle
                if bundle is not None:
                    current_mode = str(bundle.app_state.get().permission_mode or "")
                    if current_mode != self._last_permission_mode:
                        self._last_permission_mode = current_mode
                        await self._emit(BackendEvent(
                            type="plan_mode_change", plan_mode=current_mode
                        ))
                # Emit todo_update when TodoWrite tool runs
                if event.tool_name in ("TodoWrite", "todo_write"):
                    tool_input = self._last_tool_inputs.get(event.tool_name, {})
                    # TodoWrite input may have 'todos' list or markdown content field
                    todos = tool_input.get("todos") or tool_input.get("content") or []
                    if isinstance(todos, list) and todos:
                        lines = []
                        for item in todos:
                            if isinstance(item, dict):
                                checked = item.get("status", "") in ("done", "completed", "x", True)
                                text = item.get("content") or item.get("text") or str(item)
                                lines.append(f"- [{'x' if checked else ' '}] {text}")
                        if lines:
                            await self._emit_todo_markdown("\n".join(lines))
                    elif tool_input.get("item") is not None:
                        actual_path = str(
                            (event.metadata or {}).get("path")
                            or tool_input.get("path")
                            or "TODO.md"
                        )
                        await self._emit_todo_file_update(actual_path)
                    else:
                        await self._emit_todo_update_from_output(event.output)
                return
            if isinstance(event, ErrorEvent):
                await self._emit(BackendEvent(type="error", message=event.message))
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return
            if isinstance(event, StatusEvent):
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return

        async def _clear_output() -> None:
            await self._emit(BackendEvent(type="clear_transcript"))

        handle_line_kwargs: dict[str, Any] = {
            "print_system": _print_system,
            "render_event": _render_event,
            "clear_output": _clear_output,
            "render_item": _render_item,
        }
        if user_message is not None:
            handle_line_kwargs["user_message"] = user_message
        handle_line_kwargs["print_user"] = _print_user
        should_continue = await handle_line(self._bundle, line, **handle_line_kwargs)
        if is_coordinator_mode():
            await drain_coordinator_async_agents(
                self._bundle,
                prompt_seed=line,
                print_system=_print_system,
                render_event=_render_event,
            )
        await self._emit(self._status_snapshot())
        await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
        await self._refresh_plan_file_todo()
        if end_of_turn:
            await self._emit(BackendEvent(type="line_complete"))
        return should_continue

    async def _apply_select_command(self, command_name: str, value: str) -> bool:
        command = command_name.strip().lstrip("/").lower()
        selected = value.strip()
        line = build_select_command_line(command, selected)
        if line is None:
            await self._emit(BackendEvent(type="error", message=f"Unknown select command: {command_name}"))
            await self._emit(BackendEvent(type="line_complete"))
            return True
        return await self._process_line(line, transcript_line=f"/{command}")

    async def _run_shell_command(self, raw: str) -> None:
        """Run a user-authored ``!command`` without invoking the model."""
        assert self._bundle is not None
        command = raw.strip()
        if command.startswith("!"):
            command = command[1:].strip()
        if not command:
            await self._emit(BackendEvent(type="line_complete"))
            return

        from openharness.tools.base import ToolExecutionContext
        from openharness.tools.bash_tool import BashTool, BashToolInput

        await self._emit(
            BackendEvent(
                type="transcript_item",
                item=TranscriptItem(role="user", text=f"! {command}"),
            )
        )
        tool_input = {"command": command}
        self._last_tool_inputs["bash"] = tool_input
        await self._emit(
            BackendEvent(
                type="tool_started",
                tool_name="bash",
                tool_input=tool_input,
                item=TranscriptItem(
                    role="tool",
                    text=f"bash {json.dumps(tool_input, ensure_ascii=True)}",
                    tool_name="bash",
                    tool_input=tool_input,
                ),
            )
        )
        try:
            result = await BashTool().execute(
                BashToolInput(command=command, timeout_seconds=120),
                ToolExecutionContext(cwd=Path(self._bundle.cwd)),
            )
        except Exception as exc:  # noqa: BLE001 - surface as tool failure
            await self._emit(
                BackendEvent(
                    type="tool_completed",
                    tool_name="bash",
                    output=f"{type(exc).__name__}: {exc}",
                    is_error=True,
                    item=TranscriptItem(
                        role="tool_result",
                        text=f"{type(exc).__name__}: {exc}",
                        tool_name="bash",
                        is_error=True,
                    ),
                )
            )
            await self._emit(self._status_snapshot())
            await self._emit(BackendEvent(type="line_complete"))
            return

        await self._emit(
            BackendEvent(
                type="tool_completed",
                tool_name="bash",
                output=result.output,
                is_error=result.is_error,
                item=TranscriptItem(
                    role="tool_result",
                    text=result.output,
                    tool_name="bash",
                    is_error=result.is_error,
                ),
            )
        )
        await self._emit(self._status_snapshot())
        await self._emit(BackendEvent(type="line_complete"))

    def _notify_turn_complete(self, message_start: int, duration_seconds: float) -> None:
        """Send a best-effort desktop notification after a completed turn."""
        if self._bundle is None:
            return
        messages = self._bundle.engine.messages[message_start:]
        assistant = next(
            (
                message
                for message in reversed(messages)
                if message.role == "assistant" and message.text.strip()
            ),
            None,
        )
        if assistant is None:
            return
        from openharness.notifications import send_completion_notification

        cwd = self._config.cwd or self._bundle.cwd
        project = Path(cwd).name or "workspace"
        send_completion_notification(
            f"Shim · {project}",
            assistant.text,
            settings=self._bundle.current_settings().notifications,
            duration_seconds=duration_seconds,
        )

    async def _emit_tool_output(
        self, tool_name: str, tool_use_id: str, chunk: str
    ) -> None:
        """Forward a partial output preview from a running tool to the TUI."""
        if not chunk:
            return
        await self._emit(
            BackendEvent(
                type="tool_output_delta",
                tool_name=tool_name,
                tool_use_id=tool_use_id or None,
                message=chunk,
            )
        )

    def _status_snapshot(self) -> BackendEvent:
        assert self._bundle is not None
        from openharness.services.compact import estimate_message_tokens
        from openharness.utils.tokens import estimate_tokens

        usage = self._bundle.engine.total_usage
        settings = self._bundle.current_settings()
        context_window = (
            settings.context_window_tokens
            or settings.memory.context_window_tokens
        )
        # The model request also carries the system prompt and tool schemas;
        # counting only the conversation made the meter read far below the
        # real occupancy (e.g. 581/200k while ~5k was actually in flight).
        # A running turn appends to its own working list and copies it into the
        # session at the end, so the plain ``messages`` view is a turn behind.
        history = getattr(self._bundle.engine, "working_messages", None)
        if history is None:
            history = self._bundle.engine.messages
        context_tokens = estimate_message_tokens(history)
        system_prompt = self._bundle.engine.system_prompt or ""
        if system_prompt:
            context_tokens += estimate_tokens(system_prompt)
        tool_schemas = self._bundle.engine.visible_tool_schemas()
        if tool_schemas:
            context_tokens += estimate_tokens(json.dumps(tool_schemas, ensure_ascii=False))
        cache = self._bundle.engine.cache_summary
        return BackendEvent.status_snapshot(
            state=self._bundle.app_state.get(),
            mcp_servers=self._bundle.mcp_manager.list_statuses(),
            bridge_sessions=get_bridge_manager().list_sessions(),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_hit_rate=cache.get("cache_hit_rate"),
            cold_start_count=int(cache.get("cold_starts", 0)),
            cache_drift_count=(
                int(cache.get("system_drifts", 0))
                + int(cache.get("tool_drifts", 0))
                + int(cache.get("history_rewrites", 0))
            ),
            context_tokens=context_tokens,
            context_window_tokens=context_window,
        )

    def _read_checklist(self, relative_path: str) -> str:
        """Return the checklist lines of a plan/TODO file (empty when missing)."""
        assert self._bundle is not None
        path = Path(relative_path)
        if not path.is_absolute():
            path = Path(self._bundle.cwd) / path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return "\n".join(
            line for line in content.splitlines() if line.strip().startswith("- [")
        )

    async def _emit_todo_markdown(self, markdown: str) -> None:
        """Push a checklist to the panel and remember it for deduplication."""
        self._last_todo_markdown = markdown
        await self._emit(BackendEvent(type="todo_update", todo_markdown=markdown))

    async def _emit_todo_file_update(self, relative_path: str) -> None:
        """Emit the checklist from a TODO markdown file after a todo_write."""
        checklist = self._read_checklist(relative_path)
        if checklist:
            await self._emit_todo_markdown(checklist)

    async def _emit_plan_file_todo(self) -> None:
        """Repaint the panel from the session plan file, clearing it when empty."""
        assert self._bundle is not None
        plan_file = str(self._bundle.engine.tool_metadata.get("plan_file") or "")
        checklist = self._read_checklist(plan_file) if plan_file else ""
        await self._emit_todo_markdown(checklist)

    async def _refresh_plan_file_todo(self) -> None:
        """Repaint the panel when the plan file or its checklist changed.

        ``/resume`` swaps ``tool_metadata["plan_file"]`` in place, and commands
        such as ``/todos clear`` rewrite the file without moving it, so both the
        path and the content decide whether a repaint is needed.
        """
        assert self._bundle is not None
        plan_file = str(self._bundle.engine.tool_metadata.get("plan_file") or "")
        checklist = self._read_checklist(plan_file) if plan_file else ""
        if plan_file == self._last_plan_file and checklist == self._last_todo_markdown:
            return
        self._last_plan_file = plan_file
        await self._emit_todo_markdown(checklist)

    async def _emit_todo_update_from_output(self, output: str) -> None:
        """Emit a todo_update event by extracting markdown checklist from tool output."""
        # TodoWrite tools typically echo back the written content
        # We look for markdown checklist patterns in the output
        lines = output.splitlines()
        checklist_lines = [line for line in lines if line.strip().startswith("- [")]
        if checklist_lines:
            await self._emit_todo_markdown("\n".join(checklist_lines))

    def _emit_swarm_status(
        self, teammates: list[dict[str, Any]], notifications: list[dict[str, Any]] | None = None
    ) -> None:
        """Emit a swarm_status event synchronously (schedule as coroutine)."""
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(
            self._emit(BackendEvent(type="swarm_status", swarm_teammates=teammates, swarm_notifications=notifications))
        )

    async def _handle_list_sessions(self) -> None:
        import time as _time

        assert self._bundle is not None
        # A generous limit: the picker filters client-side, so the backend
        # should hand over enough history to actually search through.
        sessions = self._bundle.session_backend.list_snapshots(self._bundle.cwd, limit=50)
        current_id = self._bundle.session_id
        options = []
        for s in sessions:
            ts = _time.strftime("%m/%d %H:%M", _time.localtime(s["created_at"]))
            # Picker labels are one line by construction: never let a summary
            # with embedded newlines (or a stale harness context block) turn a
            # row into a paragraph.
            summary = " ".join(str(s.get("summary", "")).split())[:70] or "(no summary)"
            option: dict[str, object] = {
                "value": s["session_id"],
                "label": f"{ts}  {s['message_count']}msg  {summary}",
                "description": s.get("model", ""),
            }
            if s["session_id"] == current_id:
                option["active"] = True
            options.append(option)
        await self._emit(
            BackendEvent(
                type="select_request",
                modal={
                    "kind": "select",
                    "title": f"Resume Session ({len(options)})",
                    "command": "resume",
                    "searchable": True,
                },
                select_options=options,
            )
        )

    async def _handle_select_command(self, command_name: str) -> None:
        assert self._bundle is not None
        command = command_name.strip().lstrip("/").lower()
        if command == "resume":
            await self._handle_list_sessions()
            return

        prompt = build_select_prompt(
            command,
            settings=self._bundle.current_settings(),
            state=self._bundle.app_state.get(),
            max_turns=self._bundle.engine.max_turns,
        )
        if prompt is None:
            await self._emit(
                BackendEvent(type="error", message=f"No selector available for /{command}")
            )
            return
        await self._emit(
            BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": prompt.title, "command": prompt.command},
                select_options=prompt.options,
            )
        )

    async def _ask_permission(self, tool_name: str, reason: str) -> bool:
        async with self._permission_lock:
            request_id = uuid4().hex
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._permission_requests[request_id] = future
            await self._emit(
                BackendEvent(
                    type="modal_request",
                    modal={
                        "kind": "permission",
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "reason": reason,
                    },
                )
            )
            try:
                return await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                log.warning("Permission request %s timed out after 300s, denying", request_id)
                return False
            finally:
                self._permission_requests.pop(request_id, None)

    def _current_permission_mode(self) -> str:
        if self._bundle is None:
            return str(self._config.permission_mode or "")
        return str(self._bundle.app_state.get().permission_mode or "")

    async def _ask_edit_approval(self, path: str, diff: str, added: int, removed: int) -> str:
        if self._edit_always_approved or self._current_permission_mode() == "full_auto":
            return "always"

        async with self._permission_lock:
            request_id = uuid4().hex
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._edit_approval_requests[request_id] = future
            await self._emit(
                BackendEvent(
                    type="modal_request",
                    modal={
                        "kind": "edit_diff",
                        "request_id": request_id,
                        "path": path,
                        "diff": diff,
                        "added": added,
                        "removed": removed,
                    },
                )
            )
            try:
                reply = await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                log.warning("Edit approval request %s timed out after 300s, denying", request_id)
                reply = "reject"
            finally:
                self._edit_approval_requests.pop(request_id, None)
                await self._emit(BackendEvent(type="modal_request", modal=None))

            if reply == "always":
                self._edit_always_approved = True
            return reply

    async def _ask_question(
        self,
        question: str,
        options: list[str] | None = None,
    ) -> str:
        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._question_requests[request_id] = future
        modal: dict[str, object] = {
            "kind": "question",
            "request_id": request_id,
            "question": question,
        }
        cleaned_options = [str(option) for option in (options or []) if str(option).strip()]
        if cleaned_options:
            modal["options"] = cleaned_options
        await self._emit(
            BackendEvent(
                type="modal_request",
                modal=modal,
            )
        )
        try:
            return await future
        finally:
            self._question_requests.pop(request_id, None)

    async def _emit(self, event: BackendEvent) -> None:
        log.debug("emit event: type=%s tool=%s", event.type, getattr(event, "tool_name", None))
        async with self._write_lock:
            payload = _PROTOCOL_PREFIX + event.model_dump_json() + "\n"
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                buffer.write(payload.encode("utf-8"))
                buffer.flush()
                return
            sys.stdout.write(payload)
            sys.stdout.flush()


def _build_user_message_with_images(
    line: str,
    images: list[FrontendImageAttachment],
) -> ConversationMessage | None:
    if not images:
        return None
    content: list[ContentBlock] = [
        TextBlock(text=line or "Please analyze the attached image.")
    ]
    content.extend(
        ImageBlock(
            media_type=image.media_type,
            data=image.data,
            source_path=image.source_path or "",
        )
        for image in images
    )
    return ConversationMessage.from_user_content(content)


def _format_transcript_line(line: str, images: list[FrontendImageAttachment]) -> str:
    if not images:
        return line
    noun = "image" if len(images) == 1 else "images"
    attachment_line = f"[{len(images)} {noun} attached]"
    return f"{line}\n{attachment_line}" if line else attachment_line


async def run_backend_host(
    *,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    cwd: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    restore_messages: list[dict[str, Any]] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    enforce_max_turns: bool = True,
    permission_mode: str | None = None,
    session_backend: SessionBackend | None = None,
    extra_skill_dirs: tuple[str | Path, ...] = (),
    extra_plugin_roots: tuple[str | Path, ...] = (),
    memory_backend: MemoryCommandBackend | None = None,
    include_project_memory: bool = True,
    autodream_context: dict[str, object] | None = None,
) -> int:
    """Run the structured React backend host."""
    if cwd:
        os.chdir(cwd)
    host = ReactBackendHost(
        BackendHostConfig(
            model=model,
            max_turns=max_turns,
            effort=effort,
            personality=personality,
            tool_profile=tool_profile,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            active_profile=active_profile,
            api_client=api_client,
            cwd=cwd,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=enforce_max_turns,
            permission_mode=permission_mode,
            session_backend=session_backend,
            extra_skill_dirs=tuple(str(Path(path).expanduser().resolve()) for path in extra_skill_dirs),
            extra_plugin_roots=tuple(str(Path(path).expanduser().resolve()) for path in extra_plugin_roots),
            memory_backend=memory_backend,
            include_project_memory=include_project_memory,
            autodream_context=autodream_context,
        )
    )
    return await host.run()


__all__ = ["BackendHostConfig", "ReactBackendHost", "run_backend_host"]


def _edit_approval_reply_from_request(request: FrontendRequest) -> str:
    reply = (request.permission_reply or "").strip().lower()
    if reply in {"once", "always", "reject"}:
        return reply
    return "once" if bool(request.allowed) else "reject"
