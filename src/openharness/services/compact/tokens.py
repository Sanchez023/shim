"""Token estimation and auto-compact threshold helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
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
            elif isinstance(block, ImageBlock):
                total += image_token_estimate
    return int(total * TOKEN_ESTIMATION_PADDING)


def estimate_conversation_tokens(messages: list[ConversationMessage]) -> int:
    """Alias kept for backward compatibility."""
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
) -> bool:
    """Return True when the conversation should be auto-compacted."""
    if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return False
    token_count = estimate_message_tokens(messages)
    threshold = get_autocompact_threshold(
        model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    )
    return token_count >= threshold
