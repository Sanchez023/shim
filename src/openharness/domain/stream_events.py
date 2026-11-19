"""Events yielded by the query engine (layer-0, ADR-0010 D2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot


@dataclass(frozen=True)
class AssistantTextDelta:
    """Incremental assistant text."""

    text: str


@dataclass(frozen=True)
class AssistantThinkingDelta:
    """Incremental assistant reasoning/thinking text.

    This is separate from :class:`AssistantTextDelta` so answer consumers do
    not accidentally mix reasoning into the visible response.
    """

    text: str


@dataclass(frozen=True)
class AssistantTurnComplete:
    """Completed assistant turn."""

    message: ConversationMessage
    usage: UsageSnapshot


@dataclass(frozen=True)
class ToolExecutionStarted:
    """The engine is about to execute a tool."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str = ""


@dataclass(frozen=True)
class ToolExecutionCompleted:
    """A tool has finished executing."""

    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    tool_use_id: str = ""


@dataclass(frozen=True)
class ErrorEvent:
    """An error that should be surfaced to the user."""

    message: str
    recoverable: bool = True


@dataclass(frozen=True)
class StatusEvent:
    """A transient system status message shown to the user."""

    message: str


@dataclass(frozen=True)
class CompactProgressEvent:
    """Structured progress event for conversation compaction."""

    phase: Literal[
        "hooks_start",
        "context_collapse_start",
        "context_collapse_end",
        "session_memory_start",
        "session_memory_end",
        "compact_start",
        "compact_retry",
        "compact_end",
        "compact_failed",
    ]
    trigger: Literal["auto", "manual", "reactive"]
    message: str | None = None
    attempt: int | None = None
    checkpoint: str | None = None
    metadata: dict[str, Any] | None = None


StreamEvent = (
    AssistantTextDelta
    | AssistantThinkingDelta
    | AssistantTurnComplete
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | ErrorEvent
    | StatusEvent
    | CompactProgressEvent
)
