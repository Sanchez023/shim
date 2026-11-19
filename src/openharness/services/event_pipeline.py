"""Assemble the default event pipeline for a session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openharness.domain.events import EventSequencer, EventSubscriber
from openharness.domain.messages import ConversationMessage
from openharness.services import session_storage
from openharness.services.event_log import (
    EventLogStore,
    JSONLEventLogStore,
    session_event_log_path,
)
from openharness.services.event_runtime import AsyncQueueEventSink, ProjectionRegistry
from openharness.services.projections import (
    AuditState,
    CodexReasoningState,
    MetricsState,
    SessionLogState,
)
from openharness.services.subscribers import (
    JSONLEventSubscriber,
    ProjectionSubscriber,
    UIEventSubscriber,
)


@dataclass
class EventPipeline:
    """Event fan-out, durable log and projections for one session."""

    sink: AsyncQueueEventSink
    store: EventLogStore
    projections: ProjectionRegistry
    sequencer: EventSequencer
    ui_subscriber: UIEventSubscriber | None = None
    _closed: bool = field(default=False, init=False)

    async def seed_messages(
        self,
        messages: list[ConversationMessage],
        *,
        turn_id: str = "restore",
    ) -> None:
        """Seed a fresh event log from a legacy session snapshot.

        Existing event logs are authoritative and are never overwritten.
        """
        if self.store.last_seq > 0 or not messages:
            return
        for message in messages:
            kind = "user.message" if message.role == "user" else "assistant.message"
            event = self.sequencer.next(
                kind,
                {
                    "message": message.model_dump(mode="json"),
                    "restored": True,
                },
                turn_id=turn_id,
            )
            await self.sink.emit(event)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.sink.close()


async def create_event_pipeline(
    cwd: str | Path,
    session_id: str,
    *,
    enable_ui: bool = False,
    event_log_path: str | Path | None = None,
) -> EventPipeline:
    """Create and restore the default pipeline for a session.

    Existing events are replayed into the projection registry so restored
    sessions have the same derived state as live ones.
    """
    session_dir = session_storage.get_project_session_dir(cwd)
    path = (
        Path(event_log_path)
        if event_log_path is not None
        else session_event_log_path(session_dir, session_id)
    )
    store = JSONLEventLogStore(path)
    existing = await store.read_all()

    projections = ProjectionRegistry(
        {
            "session": SessionLogState(),
            "audit": AuditState(),
            "metrics": MetricsState(),
            "codex_reasoning": CodexReasoningState(),
        }
    )
    for event in existing:
        projections.apply(event)

    ui_subscriber = UIEventSubscriber() if enable_ui else None
    subscribers: list[EventSubscriber] = [
        JSONLEventSubscriber(store),
        ProjectionSubscriber(projections),
    ]
    if ui_subscriber is not None:
        subscribers.append(ui_subscriber)

    sink = AsyncQueueEventSink(subscribers)
    sequencer = EventSequencer(session_id=session_id, start_seq=store.last_seq)
    return EventPipeline(
        sink=sink,
        store=store,
        projections=projections,
        sequencer=sequencer,
        ui_subscriber=ui_subscriber,
    )
