"""Memory lifecycle port (ADR-0010 D2).

The engine calls this port after turns; the implementation lives in
``openharness.services`` and is injected at assembly time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from openharness.domain.messages import ConversationMessage


@runtime_checkable
class MemoryPort(Protocol):
    """Session-memory and durable-memory side effects owned outside the loop."""

    def prepare_session_memory(
        self,
        cwd: str | Path,
        tool_metadata: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> Path:
        """Point compaction metadata at the session-memory file."""

    def update_session_memory(
        self,
        cwd: str | Path,
        messages: list[ConversationMessage],
        *,
        tool_metadata: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Persist a session-memory checkpoint after a turn."""

    async def extract_durable_memories(
        self,
        *,
        cwd: str | Path,
        api_client: Any,
        model: str,
        messages: list[ConversationMessage],
        max_records: int = 3,
    ) -> Any:
        """Run the optional durable-memory extraction pass."""

    def schedule_auto_dream(self, **kwargs: object) -> None:
        """Schedule background memory consolidation."""
