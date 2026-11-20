"""Leaf types shared by the query engine and the turn executor.

``QueryContext`` used to live in :mod:`openharness.engine.query`, together with
the ``run_query`` compatibility shim.  That made ``turn_executor`` -- which the
shim imports -- depend on the shim, so ``turn_executor`` had to import its own
inputs lazily.  Keeping the context in a leaf module breaks that cycle without
touching a single field: the dataclass definition below is byte-for-byte the
one that used to live in ``query.py``.

Do not add behaviour here.  This module may only import from lower layers
(``api``, ``domain``, ``engine.ports``, ``permissions``, ``tools``, ``hooks``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openharness.api.client import SupportsStreamingMessages
from openharness.domain.events import EventSequencer, EventSink
from openharness.domain.messages import ConversationMessage
from openharness.engine.ports import CompactPort
from openharness.hooks import HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[..., Awaitable[str]]

REACTIVE_COMPACT_STATUS_MESSAGE = (
    "Prompt too long; compacting conversation memory and retrying."
)


class MaxTurnsExceeded(RuntimeError):
    """Raised when the agent exceeds the configured max_turns for one user prompt."""

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"Exceeded maximum turn limit ({max_turns})")
        self.max_turns = max_turns


@dataclass
class QueryContext:
    """Context shared across a query run."""

    api_client: SupportsStreamingMessages
    tool_registry: ToolRegistry
    permission_checker: PermissionChecker
    cwd: Path
    model: str
    system_prompt: str
    max_tokens: int
    system_prompt_cacheable_prefix: str = ""
    system_prompt_section_hashes: tuple[tuple[str, str], ...] = ()
    #: Per-turn dynamic context appended to the latest user message.
    turn_context: str = ""
    effort: str | None = None
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    permission_prompt: PermissionPrompt | None = None
    ask_user_prompt: AskUserPrompt | None = None
    max_turns: int | None = 200
    hook_executor: HookExecutor | None = None
    tool_metadata: dict[str, object] | None = None
    event_sink: EventSink | None = None
    event_sequencer: EventSequencer | None = None
    session_id: str = ""
    turn_id: str = ""
    current_step_id: str = ""
    tool_executor: Any | None = None
    approval_gate: Any | None = None
    context_governor: Any | None = None
    security_policy: Any | None = None
    compact_port: CompactPort | None = None
    tool_schema_names: set[str] | None = None
    tool_append_order: tuple[str, ...] = ()
    activate_tools: Callable[[Iterable[str]], None] | None = None
    #: Messages the user typed while this turn was already streaming.  The
    #: turn loop drains them at step boundaries so follow-ups reach the model
    #: before its next request instead of waiting for the whole turn to end.
    drain_pending_user_messages: Callable[[], list[ConversationMessage]] | None = None
    #: Events registered by tool calls that can move themselves to the
    #: background (Ctrl+B).  The dispatcher adds one per running call, so a
    #: concurrent tool batch yields one event per call.
    active_tool_backgrounds: set[asyncio.Event] = field(default_factory=set)
    cache_diagnostics: Any | None = None
    #: Bracket each model request so the cache keepalive can avoid racing a
    #: real request and can tell how long the prefix has been idle.
    note_model_request_start: Callable[[], None] | None = None
    note_model_request_end: Callable[[], None] | None = None
    #: ``(tool_name, tool_use_id, chunk)`` forwarded by tools that can stream
    #: their output (currently bash) to the running UI.
    tool_output_callback: Callable[[str, str, str], Awaitable[None]] | None = None


__all__ = [
    "REACTIVE_COMPACT_STATUS_MESSAGE",
    "AskUserPrompt",
    "MaxTurnsExceeded",
    "PermissionPrompt",
    "QueryContext",
]
