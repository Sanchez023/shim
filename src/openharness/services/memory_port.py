"""Service-backed implementation of the engine memory port."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openharness.domain.messages import ConversationMessage
from openharness.services.autodream.service import schedule_auto_dream
from openharness.services.memory_extract import extract_memories_from_turn
from openharness.services.session_memory import (
    prepare_session_memory_metadata,
    update_session_memory_file,
)


class ServiceMemoryPort:
    """Delegate memory lifecycles to the file-backed service implementations."""

    def prepare_session_memory(
        self,
        cwd: str | Path,
        tool_metadata: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> Path:
        return prepare_session_memory_metadata(
            cwd, tool_metadata, session_id=session_id
        )

    def update_session_memory(
        self,
        cwd: str | Path,
        messages: list[ConversationMessage],
        *,
        tool_metadata: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> Path:
        return update_session_memory_file(
            cwd,
            messages,
            tool_metadata=tool_metadata,
            session_id=session_id,
        )

    async def extract_durable_memories(
        self,
        *,
        cwd: str | Path,
        api_client: Any,
        model: str,
        messages: list[ConversationMessage],
        max_records: int = 3,
    ) -> Any:
        return await extract_memories_from_turn(
            cwd=cwd,
            api_client=api_client,
            model=model,
            messages=messages,
            max_records=max_records,
        )

    def schedule_auto_dream(self, **kwargs: object) -> None:
        schedule_auto_dream(**kwargs)
