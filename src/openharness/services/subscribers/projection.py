"""Subscriber that feeds events into a projection registry."""

from __future__ import annotations

from openharness.domain.events import AgentEvent
from openharness.services.event_runtime import ProjectionRegistry


class ProjectionSubscriber:
    """Apply every event to the registered pure folds."""

    def __init__(self, registry: ProjectionRegistry) -> None:
        self._registry = registry

    async def on_event(self, event: AgentEvent) -> None:
        self._registry.apply(event)

    async def close(self) -> None:
        return None
