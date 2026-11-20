"""Conversation compaction — microcompact and full LLM-based summarization.

Faithfully translated from Claude Code's compaction system:
- Microcompact: clear old tool result content to reduce token count cheaply
- Full compact: call the LLM to produce a structured summary of older messages
- Auto-compact: trigger compaction automatically when token count exceeds threshold

The implementation is split into focused modules; this package re-exports the
public surface so existing ``openharness.services.compact`` imports keep working.
"""

from __future__ import annotations

from openharness.services.compact.auto import (
    auto_compact_if_needed,
    compact_messages,
    summarize_messages,
)
from openharness.services.compact.boundaries import (
    build_post_compact_messages,
    create_compact_boundary_message,
)
from openharness.services.compact.constants import (
    COMPACTABLE_TOOLS,
    TIME_BASED_MC_CLEARED_MESSAGE,
    CompactionKind,
    CompactProgressCallback,
    CompactTrigger,
)
from openharness.services.compact.conversation import compact_conversation
from openharness.services.compact.microcompact import microcompact_messages
from openharness.services.compact.prompts import (
    build_compact_summary_message,
    format_compact_summary,
    get_compact_prompt,
)
from openharness.services.compact.recovery import (
    _is_prompt_too_long_error as _is_prompt_too_long_error,
)
from openharness.services.compact.recovery import try_context_collapse
from openharness.services.compact.session_memory import try_session_memory_compaction
from openharness.services.compact.tokens import (
    AUTOCOMPACT_BUFFER_TOKENS,
    AutoCompactState,
    PromptMeasurement,
    estimate_conversation_tokens,
    estimate_message_tokens,
    expected_prompt_tokens,
    get_autocompact_threshold,
    measurement_from_metadata,
    record_prompt_measurement,
    should_autocompact,
)
from openharness.services.compact.types import CompactAttachment, CompactionResult

__all__ = [
    "AUTOCOMPACT_BUFFER_TOKENS",
    "COMPACTABLE_TOOLS",
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "AutoCompactState",
    "CompactAttachment",
    "CompactProgressCallback",
    "CompactTrigger",
    "CompactionKind",
    "CompactionResult",
    "PromptMeasurement",
    "auto_compact_if_needed",
    "build_compact_summary_message",
    "build_post_compact_messages",
    "compact_conversation",
    "compact_messages",
    "create_compact_boundary_message",
    "estimate_conversation_tokens",
    "estimate_message_tokens",
    "expected_prompt_tokens",
    "format_compact_summary",
    "get_autocompact_threshold",
    "get_compact_prompt",
    "measurement_from_metadata",
    "microcompact_messages",
    "record_prompt_measurement",
    "should_autocompact",
    "summarize_messages",
    "try_context_collapse",
    "try_session_memory_compaction",
]
