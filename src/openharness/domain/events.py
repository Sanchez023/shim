"""Durable agent event contracts (layer-0, ADR-0010 D2).

These types describe facts and ports; fan-out and projection plumbing remain
in :mod:`openharness.engine.events` so the domain layer stays I/O-free.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

EventVisibility = Literal["session_private", "user_visible"]
EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AgentEvent:
    """One append-only fact emitted by the engine."""

    event_id: str
    session_id: str
    turn_id: str
    step_id: str
    seq: int
    ts: float
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION
    visibility: EventVisibility = "user_visible"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "payload": self.payload,
            "schema_version": self.schema_version,
            "visibility": self.visibility,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentEvent:
        """Restore an event from its JSON representation."""
        payload = raw.get("payload")
        raw_visibility = raw.get("visibility")
        visibility: EventVisibility = (
            raw_visibility
            if raw_visibility in {"session_private", "user_visible"}
            else "user_visible"
        )
        return cls(
            event_id=str(raw.get("event_id") or uuid.uuid4().hex),
            session_id=str(raw.get("session_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            step_id=str(raw.get("step_id") or ""),
            seq=int(raw.get("seq") or 0),
            ts=float(raw.get("ts") or 0.0),
            kind=str(raw.get("kind") or ""),
            payload=payload if isinstance(payload, dict) else {},
            schema_version=int(raw.get("schema_version") or EVENT_SCHEMA_VERSION),
            visibility=visibility,
        )


class EventSequencer:
    """Allocate monotonically increasing event sequence numbers for a session."""

    def __init__(self, *, session_id: str = "", start_seq: int = 0) -> None:
        self.session_id = session_id
        self._seq = max(0, int(start_seq))

    @property
    def last_seq(self) -> int:
        return self._seq

    def observe(self, seq: int) -> None:
        """Advance the watermark after restoring or ingesting events."""
        self._seq = max(self._seq, int(seq))

    def next(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        turn_id: str = "",
        step_id: str = "",
        visibility: EventVisibility = "user_visible",
    ) -> AgentEvent:
        self._seq += 1
        return AgentEvent(
            event_id=uuid.uuid4().hex,
            session_id=self.session_id,
            turn_id=turn_id,
            step_id=step_id,
            seq=self._seq,
            ts=time.time(),
            kind=kind,
            payload=dict(payload or {}),
            visibility=visibility,
        )


@runtime_checkable
class EventSink(Protocol):
    """Fan events out to subscribers without owning persistence or UI."""

    async def emit(self, event: AgentEvent) -> None:
        """Publish one event."""

    async def close(self) -> None:
        """Flush and close all subscribers."""


@runtime_checkable
class EventSubscriber(Protocol):
    """An asynchronous side effect attached to the event stream."""

    async def on_event(self, event: AgentEvent) -> None:
        """Handle one event."""

    async def close(self) -> None:
        """Release resources."""


@runtime_checkable
class Projection(Protocol):
    """A deterministic in-memory fold over the event log.

    ``apply`` must not perform I/O.  Persistence and UI delivery belong to an
    :class:`EventSubscriber`.
    """

    def apply(self, event: AgentEvent) -> None:
        """Fold one event into the projection state."""

    def snapshot(self, as_of_seq: int | None = None) -> Any:
        """Return a read-only view of the state."""

    def reset(self) -> None:
        """Return to the initial state."""


class NullEventSink:
    """Drop events while preserving the interface."""

    async def emit(self, event: AgentEvent) -> None:
        del event

    async def close(self) -> None:
        return None


class NullEventSubscriber:
    """No-op subscriber used as a safe default."""

    async def on_event(self, event: AgentEvent) -> None:
        del event

    async def close(self) -> None:
        return None
