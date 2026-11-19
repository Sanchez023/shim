"""Append-only event-log stores."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from openharness.domain.events import AgentEvent
from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot
from openharness.runtime.seams.core import EventLogPort
from openharness.services.projections.session_log import SessionLogState

# Backward-compatible name; the canonical protocol is EventLogPort.
EventLogStore = EventLogPort


class InMemoryEventLogStore:
    """Event-log store used by tests and ephemeral runtimes."""

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []

    @property
    def last_seq(self) -> int:
        return self._events[-1].seq if self._events else 0

    async def append(self, event: AgentEvent) -> None:
        self._events.append(event)

    async def read_all(self) -> list[AgentEvent]:
        return list(self._events)

    async def derive_messages(self) -> list[ConversationMessage]:
        return _derive_state(self._events).derive_messages()

    async def derive_usage(self) -> UsageSnapshot:
        return _derive_state(self._events).derive_usage()

    async def close(self) -> None:
        return None


class JSONLEventLogStore:
    """Append-only JSONL event log with torn-tail tolerance."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._last_seq = 0
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_seq(self) -> int:
        return self._last_seq

    async def append(self, event: AgentEvent) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append_sync, event)
            self._last_seq = max(self._last_seq, event.seq)

    async def read_all(self) -> list[AgentEvent]:
        return await asyncio.to_thread(self._read_all_sync)

    def read_all_sync(self) -> list[AgentEvent]:
        """Read all events without an event loop (legacy resume paths)."""
        return self._read_all_sync()

    async def derive_messages(self) -> list[ConversationMessage]:
        return _derive_state(await self.read_all()).derive_messages()

    async def derive_usage(self) -> UsageSnapshot:
        return _derive_state(await self.read_all()).derive_usage()

    async def close(self) -> None:
        return None

    def _append_sync(self, event: AgentEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_all_sync(self) -> list[AgentEvent]:
        if not self._path.exists():
            return []
        events: list[AgentEvent] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    # A process crash can leave a torn trailing line.  Keep the
                    # complete prefix so recovery remains possible.
                    break
                if isinstance(raw, dict):
                    events.append(AgentEvent.from_dict(raw))
        if events:
            self._last_seq = max(self._last_seq, events[-1].seq)
        return events


def _derive_state(events: list[AgentEvent]) -> SessionLogState:
    state = SessionLogState()
    for event in events:
        state.apply(event)
    return state


def session_event_log_path(session_dir: str | Path, session_id: str) -> Path:
    """Return the canonical JSONL path for a session."""
    safe_id = session_id.strip() or "default"
    return Path(session_dir) / f"{safe_id}.events.jsonl"


def event_to_json(event: AgentEvent) -> dict[str, Any]:
    """Small helper for integrations that need a plain dict."""
    return event.to_dict()
