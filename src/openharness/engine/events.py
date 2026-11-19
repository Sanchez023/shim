"""Backward-compatible re-exports for agent event contracts.

The canonical definitions live in :mod:`openharness.domain.events`
(ADR-0010 D2). In-process fan-out implementations now live in
:mod:`openharness.services.event_runtime`.
"""

from __future__ import annotations

from openharness.domain.events import (
    EVENT_SCHEMA_VERSION,
    AgentEvent,
    EventSequencer,
    EventSink,
    EventSubscriber,
    EventVisibility,
    NullEventSink,
    NullEventSubscriber,
    Projection,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "AgentEvent",
    "EventSequencer",
    "EventSink",
    "EventSubscriber",
    "EventVisibility",
    "NullEventSink",
    "NullEventSubscriber",
    "Projection",
]
