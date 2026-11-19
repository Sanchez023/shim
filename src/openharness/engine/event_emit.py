"""Event emission helper shared by the turn loop and the tool dispatcher."""

from __future__ import annotations

from typing import Any, Protocol

from openharness.domain.events import EventSequencer, EventSink, EventVisibility


class EventContext(Protocol):
    """The slice of the query context needed to emit one canonical event."""

    event_sink: EventSink | None
    event_sequencer: EventSequencer | None
    turn_id: str
    current_step_id: str


async def emit_event(
    context: EventContext,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    step_id: str = "",
    visibility: EventVisibility = "user_visible",
) -> None:
    """Emit one canonical event when the runtime has a sink attached."""
    if context.event_sink is None or context.event_sequencer is None:
        return
    event = context.event_sequencer.next(
        kind,
        payload,
        turn_id=context.turn_id,
        step_id=step_id or context.current_step_id,
        visibility=visibility,
    )
    await context.event_sink.emit(event)
