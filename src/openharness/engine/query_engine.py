"""High-level conversation engine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.api.client import SupportsStreamingMessages
from openharness.config.settings import Settings
from openharness.domain.events import EventSequencer, EventSink, NullEventSink
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    sanitize_conversation_messages,
)
from openharness.domain.stream_events import ContextChangedEvent, StreamEvent
from openharness.domain.usage import UsageSnapshot
from openharness.engine.cache_controller import CacheController
from openharness.engine.cache_diagnostics import CacheDiagnostics
from openharness.engine.cache_warmup import CacheWarmup
from openharness.engine.large_paste import offload_large_paste_message
from openharness.engine.permission_policy import PermissionPolicy
from openharness.engine.ports import CompactPort, MemoryPort
from openharness.engine.prompt_state import PromptState
from openharness.engine.query import (
    AskUserPrompt,
    PermissionPrompt,
    QueryContext,
)
from openharness.engine.session_manager import SessionManager
from openharness.engine.tool_executor import CallableApprovalGate, LocalToolExecutor
from openharness.engine.tool_visibility import ToolVisibility
from openharness.engine.turn_executor import TurnExecutor
from openharness.engine.turn_state import remember_active_artifact, remember_user_goal
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions import PermissionMode
from openharness.permissions.checker import PermissionChecker
from openharness.runtime.registry import CapabilityBundle
from openharness.runtime.seams.execution import ApprovalGate
from openharness.runtime.seams.governance.context import build_default_context_governor
from openharness.tools.base import ToolRegistry
from openharness.tools.profiles import (
    DEFAULT_TOOL_PROFILE,
)

logger = logging.getLogger(__name__)


CoordinatorContextProvider = Callable[[], dict[str, Any]]


def _default_coordinator_context() -> dict[str, Any]:
    """Coordinator runtime state, imported lazily.

    The engine is the lower layer: importing ``coordinator`` at module scope
    would make the core depend on a feature package.  Resolving it on demand
    keeps the dependency pointing the right way.
    """
    from openharness.coordinator.coordinator_mode import get_coordinator_user_context

    return get_coordinator_user_context()


class QueryEngine:
    """Owns conversation history and the tool-aware model loop."""

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        system_prompt_cacheable_prefix: str = "",
        system_prompt_section_hashes: tuple[tuple[str, str], ...] = (),
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
        max_turns: int | None = 8,
        permission_prompt: PermissionPrompt | None = None,
        ask_user_prompt: AskUserPrompt | None = None,
        hook_executor: HookExecutor | None = None,
        tool_metadata: dict[str, object] | None = None,
        settings: Settings | None = None,
        event_sink: EventSink | None = None,
        event_sequencer: EventSequencer | None = None,
        session_id: str = "",
        capability_bundle: CapabilityBundle | None = None,
        session_manager: SessionManager | None = None,
        prompt_builder: Callable[
            [str, dict[str, object]],
            tuple[str, str] | tuple[str, str, tuple[tuple[str, str], ...]],
        ]
        | None = None,
        turn_context_builder: Callable[[str, dict[str, object]], str] | None = None,
        warmup_wait_seconds: float = 2.0,
        coordinator_context_provider: CoordinatorContextProvider | None = None,
        memory_port: MemoryPort | None = None,
        compact_port: CompactPort | None = None,
    ) -> None:
        self._coordinator_context_provider = (
            coordinator_context_provider or _default_coordinator_context
        )
        self._api_client = api_client
        self._tool_registry = tool_registry
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._prompts = PromptState(
            text=system_prompt,
            cacheable_prefix=system_prompt_cacheable_prefix,
            section_hashes=system_prompt_section_hashes,
            prompt_builder=prompt_builder,
            turn_context_builder=turn_context_builder,
        )
        self._cache = CacheController(self, warmup_wait_seconds=warmup_wait_seconds)
        self._max_tokens = max_tokens
        self._effort = settings.effort if settings is not None else None
        self._context_window_tokens = context_window_tokens
        self._auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self._max_turns = max_turns
        self._permission_prompt = permission_prompt
        self._ask_user_prompt = ask_user_prompt
        self._hook_executor = hook_executor
        self._tool_metadata = tool_metadata or {}
        self._settings = settings
        self._memory_port = memory_port
        self._compact_port = compact_port
        self._tools = ToolVisibility(
            self._tool_registry,
            profile=settings.tool_profile if settings is not None else DEFAULT_TOOL_PROFILE,
        )
        bundle = capability_bundle
        if bundle is not None and bundle.model_client is not None:
            self._api_client = bundle.model_client
        self._tool_executor = (
            bundle.tool_executor if bundle is not None and bundle.tool_executor is not None else LocalToolExecutor()
        )
        self._approval_gate: ApprovalGate | None
        if bundle is not None and bundle.approval_gate is not None:
            self._approval_gate = bundle.approval_gate
        elif permission_prompt is not None:
            self._approval_gate = CallableApprovalGate(permission_prompt)
        else:
            self._approval_gate = None
        self._context_governor = (
            bundle.context_governor
            if bundle is not None and bundle.context_governor is not None
            else build_default_context_governor()
        )
        self._permissions = PermissionPolicy(
            permission_checker,
            security_policy=bundle.security_policy if bundle is not None else None,
            on_mode_changed=self._note_permission_mode,
        )
        sink = bundle.event_sink if bundle is not None and bundle.event_sink is not None else event_sink
        self._event_sink: EventSink = sink or NullEventSink()
        resolved_session_id = (
            bundle.session_id
            if bundle is not None and bundle.session_id
            else session_id
        ) or str(self._tool_metadata.get("session_id") or "")
        self._session_id = resolved_session_id
        sequencer = (
            bundle.event_sequencer
            if bundle is not None and bundle.event_sequencer is not None
            else event_sequencer
        )
        self._event_sequencer = sequencer or EventSequencer(session_id=self._session_id)
        if not self._event_sequencer.session_id:
            self._event_sequencer.session_id = self._session_id
        event_log = bundle.event_log if bundle is not None else None
        self._session = session_manager or SessionManager(
            session_id=self._session_id,
            event_log=event_log,
        )
        if not self._session.session_id:
            self._session.session_id = self._session_id
        # The active turn's context is updated in place when a tool changes
        # the permission mode, so the very next model request in the same turn
        # sees the new system prompt (plan mode guidance, etc.).
        self._active_context: QueryContext | None = None
        # The mutable message list a running turn appends to.  It is only
        # copied into the session at turn end, so live UI estimates need this
        # view to see tool results and assistant messages as they land.
        self._active_messages: list[ConversationMessage] | None = None
        # Optional UI hook: tools that stream output (bash) call this with
        # ``(tool_name, tool_use_id, chunk)`` while they run.
        self._tool_output_callback: (
            Callable[[str, str, str], Awaitable[None]] | None
        ) = None
        # Messages typed while a turn is already streaming; the turn loop
        # drains them at the next step boundary.
        self._pending_user_messages: list[ConversationMessage] = []

    @property
    def session(self) -> SessionManager:
        """Return the session-level state owner."""
        return self._session

    @property
    def messages(self) -> list[ConversationMessage]:
        """Return the current conversation history."""
        return self._session.messages

    @property
    def working_messages(self) -> list[ConversationMessage]:
        """Return the model-visible history, including a turn in flight.

        A running turn appends to its own working list and only copies it into
        the session at the end.  Read-only observers such as the TUI context
        meter use this view so they see each assistant message and tool-result
        batch as it becomes part of the next request.
        """
        if self._active_messages is not None:
            return self._active_messages
        return self._session.working_messages

    @property
    def max_turns(self) -> int | None:
        """Return the maximum number of agentic turns per user input, if capped."""
        return self._max_turns

    @property
    def api_client(self) -> SupportsStreamingMessages:
        """Return the active API client."""
        return self._api_client

    @property
    def model(self) -> str:
        """Return the active model identifier."""
        return self._model

    @property
    def system_prompt(self) -> str:
        """Return the active system prompt."""
        return self._prompts.text

    @property
    def system_prompt_cacheable_prefix(self) -> str:
        """Return the contiguous cacheable prefix of the stable system prompt."""
        return self._prompts.cacheable_prefix

    @property
    def system_prompt_section_hashes(self) -> tuple[tuple[str, str], ...]:
        """Return the per-section hashes used for cache attribution."""
        return self._prompts.section_hashes

    @property
    def effort(self) -> str | None:
        """Return the reasoning effort currently applied to requests."""
        return self._effort

    @property
    def turn_context(self) -> str:
        """Return the dynamic context attached to the latest user message."""
        return self._prompts.turn_context

    @property
    def tool_metadata(self) -> dict[str, object]:
        """Return the mutable tool metadata/carry-over state."""
        return self._tool_metadata

    @property
    def total_usage(self) -> UsageSnapshot:
        """Return the total usage across all turns."""
        return self._session.total_usage

    def clear(self) -> None:
        """Clear the in-memory conversation history."""
        self._session.clear()

    def queue_user_message(self, message: ConversationMessage) -> bool:
        """Queue a message typed while the current turn is still streaming.

        Returns ``False`` when no turn is active — the caller should submit the
        message normally instead.  Queued messages are injected at the next step
        boundary, so a follow-up reaches the model before its next request
        instead of waiting for the whole turn (tool loop included) to finish.
        """
        if self._active_context is None:
            return False
        self._pending_user_messages.append(message)
        return True

    def take_pending_user_messages(self) -> list[ConversationMessage]:
        """Follow-ups queued mid-turn that the turn never got to inject."""
        return self._drain_pending_user_messages()

    def _drain_pending_user_messages(self) -> list[ConversationMessage]:
        if not self._pending_user_messages:
            return []
        pending = self._pending_user_messages
        self._pending_user_messages = []
        return pending

    def request_background(self) -> bool:
        """Ask the tool call that is running right now to move to the background.

        Returns ``False`` when no tool is executing, i.e. there is nothing to
        background (the model is only generating text).
        """
        context = self._active_context
        if context is None or not context.active_tool_backgrounds:
            return False
        for event in list(context.active_tool_backgrounds):
            event.set()
        return True

    def set_system_prompt(
        self,
        prompt: str,
        cacheable_prefix: str | None = None,
        section_hashes: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        """Update the active system prompt, including the current turn.

        The cacheable prefix must be refreshed together with the text.  Passing
        only the text leaves Anthropic's explicit cache breakpoint pointing at
        a stale boundary, which silently disables system-prompt caching after
        any runtime prompt rebuild (plan mode, /effort, /fast, ...).
        """
        self._prompts.set(prompt, cacheable_prefix, section_hashes)
        if self._active_context is not None:
            self._active_context.system_prompt = self._prompts.text
            if cacheable_prefix is not None:
                self._active_context.system_prompt_cacheable_prefix = (
                    self._prompts.cacheable_prefix
                )
            if section_hashes is not None:
                self._active_context.system_prompt_section_hashes = (
                    self._prompts.section_hashes
                )

    def set_system_prompt_result(self, result: Any) -> None:
        """Update text, cacheable prefix and section hashes from a build result."""
        self.set_system_prompt(
            result.text,
            result.cacheable_prefix,
            tuple(getattr(result, "section_hashes", ())),
        )

    def refresh_turn_context(self) -> None:
        """Rebuild the per-turn context after a runtime setting change.

        The system prompt is intentionally frozen for the session; only the
        context attached to the latest user message changes.  This is called
        when plan mode, /fast, /effort or /passes changes mid-turn.
        """
        self._prompts.refresh_turn_context(self._tool_metadata)
        if self._active_context is not None:
            self._active_context.turn_context = self._prompts.turn_context

    def set_model(self, model: str) -> None:
        """Update the active model for future turns."""
        self._model = model

    @property
    def tool_profile(self) -> str:
        """Return the active tool visibility profile."""
        return self._tools.profile

    @property
    def visible_tool_names(self) -> set[str] | None:
        """Return visible tool names, or None when the full profile is active."""
        return self._tools.active_names

    def visible_tool_schemas(self) -> list[dict[str, Any]]:
        """Return the schemas that would be sent to the model now."""
        return self._tools.schemas()

    def _tool_append_order(self) -> tuple[str, ...]:
        """Return the deferred-tool append order for the current profile."""
        return self._tools.append_order

    def activate_tools(self, names: Iterable[str]) -> None:
        """Enable deferred tools for the rest of this engine session.

        Activation order is append-only.  Re-sorting a newly activated tool
        into the middle of the wire schema would change the provider's cached
        tool prefix for every later request.
        """
        self._tools.activate(names)
        # The turn's context snapshots the append order so the next model
        # request in the same turn sees the newly activated tool.
        if self._active_context is not None:
            self._active_context.tool_append_order = self._tools.append_order

    def set_tool_profile(self, profile: str) -> str:
        """Switch tool visibility profile and return the normalized name."""
        normalized = self._tools.set_profile(profile)
        if self._active_context is not None:
            self._active_context.tool_schema_names = self._tools.active_names
            self._active_context.tool_append_order = self._tools.append_order
        return normalized

    @property
    def cache_diagnostics(self) -> CacheDiagnostics:
        """Return per-session cache attribution counters."""
        return self._cache.diagnostics

    @property
    def cache_summary(self) -> dict[str, Any]:
        """Return overall, hot-request and drift counters for /usage."""
        return self._cache.cache_summary

    def set_cache_warmup(self, warmup: CacheWarmup | None) -> None:
        """Attach a warmup controller for implicit-cache providers."""
        self._cache.set_cache_warmup(warmup)

    def set_tool_output_callback(
        self,
        callback: Callable[[str, str, str], Awaitable[None]] | None,
    ) -> None:
        """Stream partial tool output to the UI (``tool_name, tool_use_id, chunk``)."""
        self._tool_output_callback = callback

    def set_warmup_task(self, task: asyncio.Task[None]) -> None:
        """Track the background warmup so the first turn can briefly wait."""
        self._cache.set_warmup_task(task)

    async def _wait_for_warmup(self) -> None:
        """Wait briefly for the startup warmup on the first turn."""
        await self._cache.wait_for_warmup()

    @property
    def warmup_stats(self) -> dict[str, int] | None:
        """Return warmup accounting, kept separate from real request usage."""
        return self._cache.warmup_stats

    async def warmup_cache(self, *, force: bool = False) -> None:
        """Write the current system+tools prefix into the provider cache."""
        await self._cache.warmup_cache(force=force)

    def note_model_request_start(self) -> None:
        """Record that a real model request is in flight."""
        self._cache.note_model_request_start()

    def note_model_request_end(self) -> None:
        """Record that a real model request finished (or failed)."""
        self._cache.note_model_request_end()

    @property
    def last_model_request_at(self) -> float:
        """Return the monotonic time of the last model request, or 0."""
        return self._cache.last_model_request_at

    def start_cache_refresh(
        self, idle_seconds: float = 10 * 60
    ) -> asyncio.Task[None] | None:
        """Start the opt-in prefix keepalive and return its task."""
        return self._cache.start_keepalive(idle_seconds)

    def stop_cache_refresh(self) -> asyncio.Task[None] | None:
        """Cancel the keepalive task and return it for optional awaiting."""
        return self._cache.stop_keepalive()

    @property
    def keepalive_stats(self) -> dict[str, float] | None:
        """Return keepalive counters, or ``None`` when it is not running."""
        return self._cache.keepalive_stats

    def set_effort(self, effort: str | None) -> None:
        """Update the active reasoning effort for future turns."""
        self._effort = effort

    def set_api_client(self, api_client: SupportsStreamingMessages) -> None:
        """Update the active API client for future turns."""
        self._api_client = api_client

    def set_max_turns(self, max_turns: int | None) -> None:
        """Update the maximum number of agentic turns per user input."""
        self._max_turns = None if max_turns is None else max(1, int(max_turns))

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        """Update the active permission checker for future turns."""
        self._permissions.set_checker(checker)

    @property
    def permission_mode(self) -> PermissionMode:
        """Return the live permission mode used by the current turn."""
        return self._permissions.mode

    @property
    def security_policy(self) -> object | None:
        """Return the security policy the current turn evaluates against."""
        return self._permissions.security_policy

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Switch the live permission mode, including the current turn."""
        self._permissions.set_mode(mode)
        if self._settings is not None:
            self._settings.permission.set_mode(mode)

    def _note_permission_mode(self, mode: PermissionMode) -> None:
        # Tools such as todo_write read this metadata to decide whether the
        # session plan or the repository file is the right target.
        self._tool_metadata["permission_mode"] = mode.value

    async def _after_turn_governance(self) -> None:
        if self._context_governor is not None:
            with contextlib.suppress(Exception):
                await self._context_governor.after_turn(
                    self._session.messages, self.total_usage
                )

    async def _emit_event(
        self,
        kind: str,
        payload: dict[str, object] | None = None,
        *,
        turn_id: str = "",
        step_id: str = "",
        visibility: str = "user_visible",
    ) -> None:
        await self._event_sink.emit(
            self._event_sequencer.next(
                kind,
                payload,
                turn_id=turn_id,
                step_id=step_id,
                visibility=visibility,  # type: ignore[arg-type]
            )
        )

    async def _commit_messages(
        self,
        query_messages: list[ConversationMessage],
    ) -> None:
        """Commit the turn, preferring the event log as the durable source.

        With an attached event log, the session cache is rebuilt from the log
        instead of trusting the mutable working list. This is the SoT switch:
        the log owns the facts; ``SessionManager`` is a derived view.
        """
        if self._session.event_log is not None:
            await self._session.restore_from_event_log()
            return
        self._session.replace(self._durable_messages(query_messages))

    @staticmethod
    def _durable_messages(
        messages: list[ConversationMessage],
    ) -> list[ConversationMessage]:
        """Drop per-turn runtime context from durable session state.

        The coordinator context is rebuilt for every model request, so keeping
        it in ``self._messages`` would duplicate it and diverge from the event
        log.  It remains model-visible for the current request only.
        """
        return [
            message
            for message in messages
            if not (
                message.role == "user"
                and message.text.startswith("# Coordinator User Context")
            )
        ]

    def _build_coordinator_context_message(self) -> ConversationMessage | None:
        """Build a synthetic user message carrying coordinator runtime context."""
        context = self._coordinator_context_provider()
        worker_tools_context = context.get("workerToolsContext")
        if not worker_tools_context:
            return None
        return ConversationMessage(
            role="user",
            content=[TextBlock(text=f"# Coordinator User Context\n\n{worker_tools_context}")],
        )

    def load_messages(self, messages: list[ConversationMessage]) -> None:
        """Replace the in-memory conversation history."""
        self._session.load_messages(messages)

    def _schedule_auto_dream(self) -> None:
        """Fire-and-forget background memory consolidation after a user turn."""
        if self._settings is None or self._memory_port is None:
            return
        context = self._tool_metadata.get("autodream_context")
        kwargs = dict(context) if isinstance(context, dict) else {}
        self._memory_port.schedule_auto_dream(
            cwd=self._cwd,
            settings=self._settings,
            model=self._model,
            current_session_id=str(self._tool_metadata.get("session_id") or ""),
            **kwargs,
        )

    def _prepare_session_memory(self) -> None:
        """Expose file-backed session memory to compaction when enabled."""

        if self._settings is None or self._memory_port is None:
            return
        if not self._settings.memory.session_memory_enabled:
            return
        if not self._settings.memory.enabled:
            return
        self._memory_port.prepare_session_memory(
            self._cwd,
            self._tool_metadata,
            session_id=str(self._tool_metadata.get("session_id") or "default"),
        )

    async def _update_session_memory(self) -> None:
        """Persist a session checkpoint after a user turn."""

        if self._settings is None or self._memory_port is None:
            return
        if not self._settings.memory.session_memory_enabled:
            return
        if not self._settings.memory.enabled:
            return
        self._memory_port.update_session_memory(
            self._cwd,
            self._session.messages,
            tool_metadata=self._tool_metadata,
            session_id=str(self._tool_metadata.get("session_id") or "default"),
        )

    async def _extract_durable_memories(self) -> None:
        """Run the optional durable memory extraction pass."""

        if self._settings is None or self._memory_port is None:
            return
        if not self._settings.memory.auto_extract_enabled:
            return
        if not self._settings.memory.enabled:
            return
        try:
            result = await self._memory_port.extract_durable_memories(
                cwd=self._cwd,
                api_client=self._api_client,
                model=self._model,
                messages=self._session.messages,
                max_records=self._settings.memory.auto_extract_max_records,
            )
        except Exception as exc:
            logger.warning("Memory extraction failed", exc_info=True)
            self._tool_metadata["memory_extract_last_error"] = str(exc)
            return
        self._tool_metadata["memory_extract_last"] = {
            "skipped": result.skipped,
            "reason": result.reason,
            "written_paths": [str(path) for path in result.written_paths],
        }

    def has_pending_continuation(self) -> bool:
        """Return True when the conversation ends with tool results awaiting a follow-up model turn."""
        messages = self._session.messages
        if not messages:
            return False
        last = messages[-1]
        if last.role != "user":
            return False
        if not any(isinstance(block, ToolResultBlock) for block in last.content):
            return False
        for msg in reversed(messages[:-1]):
            if msg.role != "assistant":
                continue
            return bool(msg.tool_uses)
        return False

    def _build_query_context(
        self,
        *,
        turn_id: str,
        max_turns: int | None,
    ) -> QueryContext:
        """Build the per-turn context in one place.

        The submit and continuation paths used to duplicate this block; the
        only legitimate difference is the turn-limit override. Keeping the
        override explicit prevents a continuation from silently falling back
        to the engine default.
        """
        return QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permissions.checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._prompts.text,
            system_prompt_cacheable_prefix=self._prompts.cacheable_prefix,
            system_prompt_section_hashes=self._prompts.section_hashes,
            turn_context=self._prompts.turn_context,
            max_tokens=self._max_tokens,
            effort=self._effort,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
            event_sink=self._event_sink,
            event_sequencer=self._event_sequencer,
            session_id=self._session_id,
            turn_id=turn_id,
            tool_executor=self._tool_executor,
            approval_gate=self._approval_gate,
            context_governor=self._context_governor,
            compact_port=self._compact_port,
            security_policy=self._permissions.security_policy,
            tool_schema_names=self._tools.active_names,
            tool_append_order=self._tool_append_order(),
            activate_tools=self.activate_tools,
            drain_pending_user_messages=self._drain_pending_user_messages,
            cache_diagnostics=self._cache.diagnostics,
            note_model_request_start=self.note_model_request_start,
            note_model_request_end=self.note_model_request_end,
            tool_output_callback=self._tool_output_callback,
        )

    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        """Append a user message and execute the query loop."""
        await self._wait_for_warmup()
        user_message = (
            prompt
            if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        # Context building and goal tracking use what the user actually typed,
        # even when an oversized paste is later replaced by a preview.
        original_text = user_message.text
        self._prompts.prepare_turn(original_text, self._tool_metadata)
        if original_text.strip() and not self._tool_metadata.pop("_suppress_next_user_goal", False):
            remember_user_goal(self._tool_metadata, original_text)
        paste_artifact: Path | None = None
        if self._settings is not None and self._settings.offload_large_pastes:
            user_message, paste_artifact = offload_large_paste_message(
                user_message,
                threshold_chars=self._settings.large_paste_threshold_chars,
                preview_chars=self._settings.large_paste_preview_chars,
            )
            if paste_artifact is not None:
                remember_active_artifact(self._tool_metadata, str(paste_artifact))
                self._tool_metadata["last_paste_artifact"] = str(paste_artifact)
        self._prepare_session_memory()
        self._session.replace(sanitize_conversation_messages(self._session.messages))
        turn_id = uuid4().hex
        turn_start_payload: dict[str, object] = {
            "prompt": user_message.text,
        }
        if paste_artifact is not None:
            turn_start_payload["paste_artifact"] = str(paste_artifact)
            turn_start_payload["original_chars"] = len(original_text)
        await self._emit_event(
            "turn.start",
            turn_start_payload,
            turn_id=turn_id,
        )
        # Persist the per-turn context as a private user message.  It is part
        # of the model-visible history (so later requests hit it in the prompt
        # cache) but hidden from transcripts and UI projections.
        self._prompts.bind_turn_context_message(None)
        if self._prompts.turn_context:
            context_message = ConversationMessage(
                role="user",
                content=[
                    TextBlock(
                        text=(
                            f"<turn_context>\n{self._prompts.turn_context}"
                            "\n</turn_context>"
                        ),
                        visibility="session_private",
                    )
                ],
            )
            self._session.append(context_message)
            self._prompts.bind_turn_context_message(context_message)
            await self._emit_event(
                "user.message",
                {"message": context_message.model_dump(mode="json")},
                turn_id=turn_id,
                visibility="session_private",
            )
        self._session.append(user_message)
        await self._emit_event(
            "user.message",
            {"message": user_message.model_dump(mode="json")},
            turn_id=turn_id,
        )
        if self._hook_executor is not None:
            await self._hook_executor.execute(
                HookEvent.USER_PROMPT_SUBMIT,
                {
                    "event": HookEvent.USER_PROMPT_SUBMIT.value,
                    "prompt": user_message.text,
                },
            )
        context = self._build_query_context(
            turn_id=turn_id,
            max_turns=self._max_turns,
        )
        self._active_context = context
        query_messages = list(self._session.messages)
        coordinator_context = self._build_coordinator_context_message()
        if coordinator_context is not None:
            query_messages.append(coordinator_context)
        self._active_messages = query_messages
        status = "completed"
        try:
            # Let the UI refresh its context meter before waiting on the model:
            # the user prompt is already part of the model-visible history.
            yield ContextChangedEvent()
            async for event, usage in TurnExecutor(context).execute(query_messages):
                if usage is not None:
                    self._session.add_usage(usage)
                yield event
        except (GeneratorExit, asyncio.CancelledError):
            # An interrupted turn is neither a success nor an API error. Without
            # this branch the ``finally`` below reports "completed", which hides
            # the interruption from the UI and from session projections.
            status = "interrupted"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self._active_context = None
            self._active_messages = None
            # Commit only after the turn finishes so tool results appended
            # after AssistantTurnComplete are included. With an event log the
            # cache is re-derived from the log instead of the working list.
            await self._commit_messages(query_messages)
            await self._emit_event(
                "turn.end",
                {"status": status},
                turn_id=turn_id,
            )
            await self._after_turn_governance()
            await self._update_session_memory()
            await self._extract_durable_memories()
            self._schedule_auto_dream()

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        """Continue an interrupted tool loop without appending a new user message."""
        self._prepare_session_memory()
        self._session.replace(sanitize_conversation_messages(self._session.messages))
        turn_id = uuid4().hex
        await self._emit_event(
            "turn.start",
            {"prompt": "", "continuation": True},
            turn_id=turn_id,
        )
        context = self._build_query_context(
            turn_id=turn_id,
            max_turns=max_turns if max_turns is not None else self._max_turns,
        )
        self._active_context = context
        working_messages = self._session.working_messages
        self._active_messages = working_messages
        status = "completed"
        try:
            async for event, usage in TurnExecutor(context).execute(working_messages):
                if usage is not None:
                    self._session.add_usage(usage)
                yield event
        except (GeneratorExit, asyncio.CancelledError):
            status = "interrupted"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self._active_context = None
            self._active_messages = None
            await self._emit_event(
                "turn.end",
                {"status": status, "continuation": True},
                turn_id=turn_id,
            )
            await self._commit_messages(self._session.messages)
            await self._after_turn_governance()
            await self._update_session_memory()
            await self._extract_durable_memories()
