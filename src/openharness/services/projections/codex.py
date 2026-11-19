"""Projection for Codex reasoning observability without opaque payloads."""

from __future__ import annotations

from typing import Any

from openharness.domain.events import AgentEvent
from openharness.domain.messages import ConversationMessage, ReasoningBlock


class CodexReasoningState:
    """Track reasoning item metadata while never exposing encrypted content."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def apply(self, event: AgentEvent) -> None:
        self._last_seq = max(self._last_seq, event.seq)
        if event.kind != "assistant.message":
            return
        raw_message = event.payload.get("message")
        if not isinstance(raw_message, dict):
            return
        try:
            message = ConversationMessage.model_validate(raw_message)
        except (ValueError, TypeError):
            return
        for block in message.content:
            if not isinstance(block, ReasoningBlock) or block.provider != "codex":
                continue
            self._items.append(
                {
                    "seq": event.seq,
                    "item_id": block.item_id,
                    "summary": list(block.summary),
                    "has_encrypted_content": bool(block.encrypted_content),
                }
            )

    def snapshot(self, as_of_seq: int | None = None) -> list[dict[str, Any]]:
        if as_of_seq is None:
            return [dict(item) for item in self._items]
        return [dict(item) for item in self._items if item["seq"] <= as_of_seq]

    def reset(self) -> None:
        self._items = []
        self._last_seq = 0
