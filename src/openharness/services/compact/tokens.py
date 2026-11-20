"""Token estimation and auto-compact threshold helpers."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.utils.tokens import estimate_tokens

log = logging.getLogger(__name__)

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

TOKEN_ESTIMATION_PADDING = 4 / 3
_DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE = 3_072
_DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass
class AutoCompactState:
    """Mutable state that persists across query loop turns."""

    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""
    consecutive_failures: int = 0


def estimate_message_tokens(messages: list[ConversationMessage]) -> int:
    """Estimate total tokens for a conversation, including the 4/3 padding."""
    total = 0
    image_token_estimate = _vision_token_budget_per_image()
    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextBlock):
                total += estimate_tokens(block.text)
            elif isinstance(block, ToolResultBlock):
                total += estimate_tokens(block.content)
            elif isinstance(block, ToolUseBlock):
                total += estimate_tokens(block.name)
                total += estimate_tokens(str(block.input))
            elif isinstance(block, ThinkingBlock):
                # Extended-thinking blocks are replayed verbatim on every
                # request (and their signature too), so they are billed like
                # any other content. Leaving them out made the estimate
                # under-report a long reasoning session by ~35%: a 260k-token
                # prompt looked like 170k, so auto-compact fired late and then
                # kept re-firing because it could never get back under budget.
                total += estimate_tokens(block.thinking)
                total += estimate_tokens(block.signature)
            elif isinstance(block, ImageBlock):
                total += image_token_estimate
    return int(total * TOKEN_ESTIMATION_PADDING)


def estimate_conversation_tokens(messages: list[ConversationMessage]) -> int:
    """Alias kept for backward compatibility."""
    return estimate_message_tokens(messages)


#: Key under which the last provider-reported prompt size is stashed in
#: ``TurnContext.tool_metadata`` / the snapshot's ``tool_metadata``.
PROMPT_MEASUREMENT_KEY = "prompt_measurement"


@dataclass(frozen=True)
class PromptMeasurement:
    """What the provider actually charged for a known message-list prefix.

    Estimation is only a fallback: every response tells us the real prompt size
    (``usage.prompt_tokens``) for the exact message list we just sent. Anchoring
    on that number and estimating only the *delta* added since gives an error of
    a few hundred tokens instead of the systematic bias a pure estimate has.
    """

    message_count: int
    prompt_tokens: int

    @classmethod
    def from_mapping(cls, raw: object) -> PromptMeasurement | None:
        if not isinstance(raw, dict):
            return None
        try:
            message_count = int(raw.get("message_count"))  # type: ignore[arg-type]
            prompt_tokens = int(raw.get("prompt_tokens"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if message_count < 0 or prompt_tokens <= 0:
            return None
        return cls(message_count=message_count, prompt_tokens=prompt_tokens)

    def to_mapping(self) -> dict[str, int]:
        return {"message_count": self.message_count, "prompt_tokens": self.prompt_tokens}


def measurement_from_metadata(metadata: Mapping[str, object] | None) -> PromptMeasurement | None:
    """Read the last provider measurement stored alongside a turn."""
    if not metadata:
        return None
    return PromptMeasurement.from_mapping(metadata.get(PROMPT_MEASUREMENT_KEY))


def expected_prompt_tokens(
    messages: list[ConversationMessage],
    measurement: PromptMeasurement | None = None,
) -> int:
    """Best available prompt size: measured total plus an estimated delta.

    Falls back to a pure estimate when there is no measurement yet, or when the
    history was rewritten (compaction, ``/clear``) so that the measured prefix
    no longer lines up with the current list.
    """
    if measurement is not None and 0 <= measurement.message_count <= len(messages):
        delta = messages[measurement.message_count :]
        if not delta:
            return measurement.prompt_tokens
        return measurement.prompt_tokens + estimate_message_tokens(delta)
    return estimate_message_tokens(messages)


def estimate_image_tokens() -> int:
    """Estimated prompt tokens charged for one inline image block."""
    return _vision_token_budget_per_image()


def _vision_token_budget_per_image() -> int:
    raw = os.environ.get("OPENHARNESS_IMAGE_TOKEN_ESTIMATE", "").strip()
    if raw:
        try:
            return max(64, int(raw))
        except ValueError:
            log.warning("Ignoring invalid OPENHARNESS_IMAGE_TOKEN_ESTIMATE=%r", raw)
    return _DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE


def get_context_window(model: str, *, context_window_tokens: int | None = None) -> int:
    """Return the context window size for a model (conservative defaults)."""
    if context_window_tokens is not None and context_window_tokens > 0:
        return int(context_window_tokens)
    m = model.lower()
    if "opus" in m:
        return 200_000
    if "sonnet" in m:
        return 200_000
    if "haiku" in m:
        return 200_000
    if "deepseek" in m:
        # DeepSeek's published context window is 128k; using the generic 200k
        # default made auto-compact fire too late and fall back to a reactive
        # prompt-too-long compaction, which is a full cache reset.
        return 128_000
    # Kimi / other providers — be conservative
    return _DEFAULT_CONTEXT_WINDOW


def get_autocompact_threshold(
    model: str,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
) -> int:
    """Calculate the token count at which auto-compact fires."""
    if auto_compact_threshold_tokens is not None and auto_compact_threshold_tokens > 0:
        return int(auto_compact_threshold_tokens)
    context_window = get_context_window(model, context_window_tokens=context_window_tokens)
    reserved = min(MAX_OUTPUT_TOKENS_FOR_SUMMARY, 20_000)
    effective = context_window - reserved
    return effective - AUTOCOMPACT_BUFFER_TOKENS


def should_autocompact(
    messages: list[ConversationMessage],
    model: str,
    state: AutoCompactState,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
    measurement: PromptMeasurement | None = None,
) -> bool:
    """Return True when the conversation should be auto-compacted."""
    if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return False
    token_count = expected_prompt_tokens(messages, measurement)
    threshold = get_autocompact_threshold(
        model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    )
    return token_count >= threshold


def record_prompt_measurement(
    metadata: dict[str, object] | None,
    *,
    message_count: int,
    prompt_tokens: int,
) -> bool:
    """Store the provider-reported prompt size for the message list just sent.

    Returns False when there is nothing to record (no metadata dict, or a usage
    block the provider did not fill in).
    """
    if metadata is None or prompt_tokens <= 0 or message_count < 0:
        return False
    metadata[PROMPT_MEASUREMENT_KEY] = {
        "message_count": message_count,
        "prompt_tokens": prompt_tokens,
    }
    return True
