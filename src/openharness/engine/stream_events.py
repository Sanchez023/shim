"""Backward-compatible re-exports for stream events.

The canonical definitions live in :mod:`openharness.domain.stream_events`
(ADR-0010 D2).
"""

from __future__ import annotations

from openharness.domain.stream_events import (
    AssistantTextDelta,
    AssistantThinkingDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)

__all__ = [
    "AssistantTextDelta",
    "AssistantThinkingDelta",
    "AssistantTurnComplete",
    "CompactProgressEvent",
    "ErrorEvent",
    "StatusEvent",
    "StreamEvent",
    "ToolExecutionCompleted",
    "ToolExecutionStarted",
]
