"""Projection that reconstructs model-visible conversation state."""

from __future__ import annotations

from typing import Any

from openharness.domain.events import AgentEvent
from openharness.domain.messages import (
    ConversationMessage,
    sanitize_conversation_messages,
)
from openharness.domain.usage import UsageSnapshot


class SessionLogState:
    """Pure fold from events to messages and usage.

    The projection never touches disk.  Persistence is handled by the JSONL
    event-log store; this class is what makes replay and state-consistency
    checks cheap.
    """

    def __init__(self) -> None:
        self._messages: list[ConversationMessage] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_creation_input_tokens = 0
        self._cache_read_input_tokens = 0
        self._last_seq = 0
        self._turn_count = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def input_tokens(self) -> int:
        return self._input_tokens

    @property
    def output_tokens(self) -> int:
        return self._output_tokens

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def apply(self, event: AgentEvent) -> None:
        self._last_seq = max(self._last_seq, event.seq)

        if event.kind == "user.message":
            message = self._message_from_payload(event.payload.get("message"))
            if message is not None:
                self._messages.append(message)
            return

        if event.kind == "assistant.message":
            message = self._message_from_payload(event.payload.get("message"))
            if message is not None:
                self._messages.append(message)
            usage = event.payload.get("usage")
            if isinstance(usage, dict):
                self._input_tokens += int(usage.get("input_tokens") or 0)
                self._output_tokens += int(usage.get("output_tokens") or 0)
                self._cache_creation_input_tokens += int(
                    usage.get("cache_creation_input_tokens") or 0
                )
                self._cache_read_input_tokens += int(
                    usage.get("cache_read_input_tokens") or 0
                )
            return

        if event.kind == "context.compacted":
            raw_messages = event.payload.get("messages")
            if isinstance(raw_messages, list):
                self._messages = [
                    message
                    for raw in raw_messages
                    if (message := self._message_from_payload(raw)) is not None
                ]
            return

        if event.kind == "turn.end":
            self._turn_count += 1

    def snapshot(self, as_of_seq: int | None = None) -> dict[str, Any]:
        del as_of_seq
        return {
            "messages": [message.model_dump(mode="json") for message in self._messages],
            "usage": {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cache_creation_input_tokens": self._cache_creation_input_tokens,
                "cache_read_input_tokens": self._cache_read_input_tokens,
            },
            "turn_count": self._turn_count,
            "last_seq": self._last_seq,
        }

    def derive_messages(self) -> list[ConversationMessage]:
        # Apply the same normalization as the live resume path: an interrupted
        # trailing tool_use without a matching tool_result must not come back
        # when the session is replayed. The event log keeps the fact; this
        # model-visible projection drops the malformed tail.
        return sanitize_conversation_messages(list(self._messages))

    def derive_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_creation_input_tokens=self._cache_creation_input_tokens,
            cache_read_input_tokens=self._cache_read_input_tokens,
        )

    def reset(self) -> None:
        self._messages = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_creation_input_tokens = 0
        self._cache_read_input_tokens = 0
        self._last_seq = 0
        self._turn_count = 0

    @staticmethod
    def _message_from_payload(raw: Any) -> ConversationMessage | None:
        if isinstance(raw, ConversationMessage):
            return raw
        if not isinstance(raw, dict):
            return None
        try:
            return ConversationMessage.model_validate(raw)
        except (ValueError, TypeError):
            return None
