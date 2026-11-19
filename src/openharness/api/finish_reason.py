"""Normalized reason a model stopped producing output.

Every adapter used to pass the provider's raw string upward and nothing read
it, so a ``max_tokens`` cutoff was indistinguishable from a finished answer and
got persisted as one.  ``FinishReason`` gives the engine one vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


class FinishReason(StrEnum):
    """Why the model stopped."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    TOOL_USE = "tool_use"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    STOP_SEQUENCE = "stop_sequence"
    UNKNOWN = "unknown"


#: Raw provider values, keyed by the string the wire format uses.
_RAW_TO_REASON: dict[str, FinishReason] = {
    # Anthropic
    "end_turn": FinishReason.END_TURN,
    "pause_turn": FinishReason.UNKNOWN,
    "max_tokens": FinishReason.MAX_TOKENS,
    "tool_use": FinishReason.TOOL_USE,
    "stop_sequence": FinishReason.STOP_SEQUENCE,
    "refusal": FinishReason.REFUSAL,
    # OpenAI / Codex / most OpenAI-compatible servers
    "stop": FinishReason.END_TURN,
    "length": FinishReason.MAX_TOKENS,
    "tool_calls": FinishReason.TOOL_USE,
    "function_call": FinishReason.TOOL_USE,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def normalize_finish_reason(raw: str | None) -> FinishReason:
    """Map a provider's raw stop reason onto :class:`FinishReason`."""
    if not raw:
        return FinishReason.UNKNOWN
    return _RAW_TO_REASON.get(str(raw).strip().lower(), FinishReason.UNKNOWN)


def is_truncated(reason: FinishReason) -> bool:
    """True when the model was cut off rather than finished."""
    return reason is FinishReason.MAX_TOKENS
