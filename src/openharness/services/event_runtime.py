"""In-process event fan-out and projection plumbing.

These implementations own asyncio queues and therefore stay in the service
layer; the contracts live in :mod:`openharness.domain.events`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from openharness.domain.events import (
    AgentEvent,
    EventSubscriber,
    Projection,
)

log = logging.getLogger(__name__)


class AsyncQueueEventSink:
    """Fan out events to subscribers in registration order.

    Subscribers are expected to enqueue quickly; a slow subscriber applies
    backpressure to the engine, which is preferable to unbounded buffering.
    """

    def __init__(self, subscribers: list[EventSubscriber] | None = None) -> None:
        self._subscribers: list[EventSubscriber] = list(subscribers or [])
        self._closed = False

    def add_subscriber(self, subscriber: EventSubscriber) -> None:
        self._subscribers.append(subscriber)

    async def emit(self, event: AgentEvent) -> None:
        if self._closed:
            return
        for subscriber in self._subscribers:
            try:
                await subscriber.on_event(event)
            except Exception:
                log.exception(
                    "event subscriber failed: subscriber=%s kind=%s seq=%s",
                    type(subscriber).__name__,
                    event.kind,
                    event.seq,
                )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscriber in reversed(self._subscribers):
            try:
                await subscriber.close()
            except Exception:
                log.exception(
                    "event subscriber close failed: subscriber=%s",
                    type(subscriber).__name__,
                )


class AsyncQueueSubscriber:
    """An event subscriber backed by an asyncio queue for async consumers."""

    def __init__(self, *, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def on_event(self, event: AgentEvent) -> None:
        if self._closed:
            return
        await self._queue.put(event)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[AgentEvent]:
        """Yield events until the subscriber is closed."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class ProjectionRegistry:
    """Apply events to a set of named projections."""

    def __init__(self, projections: dict[str, Projection] | None = None) -> None:
        self._projections: dict[str, Projection] = dict(projections or {})
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def register(self, name: str, projection: Projection) -> None:
        self._projections[name] = projection

    def get(self, name: str) -> Projection:
        return self._projections[name]

    def apply(self, event: AgentEvent) -> None:
        self._last_seq = max(self._last_seq, event.seq)
        for projection in self._projections.values():
            projection.apply(event)

    def snapshot(self, name: str, as_of_seq: int | None = None) -> Any:
        return self._projections[name].snapshot(as_of_seq)

    def reset(self) -> None:
        for projection in self._projections.values():
            projection.reset()
        self._last_seq = 0
