"""High-level session facade over the append-only event log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot
from openharness.services.event_log import EventLogStore
from openharness.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend


class SessionStore:
    """Snapshot/list/export facade backed by an :class:`EventLogStore`.

    The event log remains the source of truth.  The legacy snapshot backend is
    kept for compatibility and for consumers that still expect file snapshots.
    """

    def __init__(
        self,
        *,
        cwd: str | Path,
        event_log: EventLogStore,
        backend: SessionBackend = DEFAULT_SESSION_BACKEND,
    ) -> None:
        self._cwd = Path(cwd)
        self._event_log = event_log
        self._backend = backend

    async def derive_messages(self) -> list[ConversationMessage]:
        return await self._event_log.derive_messages()

    async def derive_usage(self) -> UsageSnapshot:
        return await self._event_log.derive_usage()

    async def snapshot(self) -> dict[str, Any]:
        return {
            "messages": [
                message.model_dump(mode="json")
                for message in await self.derive_messages()
            ],
            "usage": (await self.derive_usage()).model_dump(mode="json"),
            "last_seq": self._event_log.last_seq,
        }

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._backend.list_snapshots(self._cwd, limit=limit)

    def load_latest(self) -> dict[str, Any] | None:
        return self._backend.load_latest(self._cwd)

    def load_by_id(self, session_id: str) -> dict[str, Any] | None:
        return self._backend.load_by_id(self._cwd, session_id)

    def save_snapshot(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
        session_id: str | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> Path:
        return self._backend.save_snapshot(
            cwd=self._cwd,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            usage=usage,
            session_id=session_id,
            tool_metadata=tool_metadata,
        )

    def export_markdown(self, messages: list[ConversationMessage]) -> Path:
        return self._backend.export_markdown(cwd=self._cwd, messages=messages)
