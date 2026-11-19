"""Subscriber that persists the event stream to an event-log store."""

from __future__ import annotations

from openharness.domain.events import AgentEvent
from openharness.runtime.seams.core import EventLogPort


class JSONLEventSubscriber:
    """Persist every event through an :class:`EventLogPort`."""

    def __init__(self, store: EventLogPort) -> None:
        self._store = store

    async def on_event(self, event: AgentEvent) -> None:
        await self._store.append(event)

    async def close(self) -> None:
        await self._store.close()
