"""Core ports and event contracts (ADR-0010 D2).

This is the single home for cross-layer protocols. Implementations live in
``openharness.services`` and are injected at assembly time; this module must
not import ``services``, ``engine``, or ``api``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openharness.domain.events import (
    AgentEvent,
    EventSink,
    EventSubscriber,
    EventVisibility,
    Projection,
)
from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot


@runtime_checkable
class EventLogPort(Protocol):
    """Durable event log used by the engine for restore/replay."""

    @property
    def last_seq(self) -> int:
        """Return the highest sequence number written so far."""

    async def append(self, event: AgentEvent) -> None:
        """Append one event."""

    async def read_all(self) -> list[AgentEvent]:
        """Read every complete event, tolerating a torn trailing line."""

    async def derive_messages(self) -> list[ConversationMessage]:
        """Reconstruct model-visible messages."""

    async def derive_usage(self) -> UsageSnapshot:
        """Reconstruct aggregate token usage."""

    async def close(self) -> None:
        """Flush resources."""


# Backward-compatible alias for callers that still use the old seam name.
EventLogStore = EventLogPort


@runtime_checkable
class SessionAppendPort(Protocol):
    """Minimal session view needed by recording backends."""

    @property
    def session_id(self) -> str:
        """Return the stable session identifier."""

    def append(self, message: ConversationMessage) -> None:
        """Append one durable message."""


__all__ = [
    "AgentEvent",
    "EventLogPort",
    "EventSink",
    "EventSubscriber",
    "EventVisibility",
    "Projection",
    "SessionAppendPort",
]
