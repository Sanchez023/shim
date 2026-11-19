"""Session-level state ownership.

``SessionManager`` is the single owner of durable conversation messages and
aggregate usage.  ``QueryEngine`` delegates to it instead of holding mutable
session state itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot
from openharness.engine.cost_tracker import CostTracker
from openharness.engine.ports import EventLogPort


class SessionManager:
    """Own conversation messages and usage for one session."""

    def __init__(
        self,
        *,
        session_id: str = "",
        messages: list[ConversationMessage] | None = None,
        event_log: EventLogPort | None = None,
        message_view: Callable[[], list[ConversationMessage]] | None = None,
        usage_view: Callable[[], UsageSnapshot] | None = None,
    ) -> None:
        self.session_id = session_id
        self._messages: list[ConversationMessage] = list(messages or [])
        self._cost_tracker = CostTracker()
        self._event_log = event_log
        # When the event transport owns an incrementally updated projection,
        # read the cache view from it instead of re-reading the whole log.
        self._message_view = message_view
        self._usage_view = usage_view

    @property
    def messages(self) -> list[ConversationMessage]:
        """Return a defensive copy of the current messages."""
        return list(self._messages)

    @property
    def working_messages(self) -> list[ConversationMessage]:
        """Return the mutable list used by the turn executor.

        Callers inside the engine may mutate this list; external callers
        should use :attr:`messages`.
        """
        return self._messages

    @property
    def total_usage(self) -> UsageSnapshot:
        return self._cost_tracker.total

    @property
    def event_log(self) -> EventLogPort | None:
        return self._event_log

    def load_messages(self, messages: list[ConversationMessage]) -> None:
        self._messages = list(messages)

    def append(self, message: ConversationMessage) -> None:
        self._messages.append(message)

    def replace(self, messages: list[ConversationMessage]) -> None:
        self._messages = list(messages)

    def clear(self) -> None:
        self._messages.clear()
        self._cost_tracker = CostTracker()

    def add_usage(self, usage: UsageSnapshot) -> None:
        self._cost_tracker.add(usage)

    async def restore_from_event_log(self) -> None:
        """Rebuild the cache from the authoritative event log.

        A live event pipeline exposes an incrementally updated projection, so
        per-turn commits do not re-read the whole log. Without one (a plain
        store), fall back to deriving from the store.
        """
        if self._event_log is None:
            return
        if self._message_view is not None:
            self._messages = list(self._message_view())
        else:
            self._messages = await self._event_log.derive_messages()
        usage = (
            self._usage_view()
            if self._usage_view is not None
            else await self._event_log.derive_usage()
        )
        self._cost_tracker = CostTracker()
        self._cost_tracker.add(usage)

    async def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages": [message.model_dump(mode="json") for message in self._messages],
            "usage": self._cost_tracker.total.model_dump(mode="json"),
        }
