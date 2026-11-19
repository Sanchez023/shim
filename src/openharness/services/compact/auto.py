"""Auto-compact integration for the query loop."""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from uuid import uuid4

from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    sanitize_conversation_messages,
)
from openharness.hooks import HookExecutor
from openharness.services.compact.boundaries import (
    _split_preserving_tool_pairs,
    build_post_compact_messages,
)
from openharness.services.compact.constants import (
    SESSION_MEMORY_KEEP_RECENT,
    CompactProgressCallback,
    CompactTrigger,
)
from openharness.services.compact.conversation import compact_conversation
from openharness.services.compact.microcompact import microcompact_messages
from openharness.services.compact.progress import _emit_progress, _record_compact_checkpoint
from openharness.services.compact.recovery import try_context_collapse
from openharness.services.compact.session_memory import try_session_memory_compaction
from openharness.services.compact.tokens import (
    AUTOCOMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    AutoCompactState,
    estimate_message_tokens,
    get_autocompact_threshold,
    should_autocompact,
)

log = logging.getLogger(__name__)

DEFAULT_COMPACT_COOLDOWN_SECONDS = 180.0


def _compact_cooldown_seconds() -> float:
    raw = os.environ.get("OPENHARNESS_COMPACT_COOLDOWN_SECONDS", "").strip()
    if not raw:
        return DEFAULT_COMPACT_COOLDOWN_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("Ignoring invalid OPENHARNESS_COMPACT_COOLDOWN_SECONDS=%r", raw)
        return DEFAULT_COMPACT_COOLDOWN_SECONDS


def _within_compact_cooldown(
    metadata: dict[str, Any] | None,
    messages: list[ConversationMessage],
    *,
    model: str,
    context_window_tokens: int | None,
    auto_compact_threshold_tokens: int | None,
) -> bool:
    """Return True when a recent compaction should suppress another one.

    Rewriting history resets the provider's prefix cache from the rewrite
    point, so a second compaction within a short window pays the cost twice
    for little context relief.  An emergency compaction is still allowed when
    the prompt is well past the threshold.
    """
    cooldown = _compact_cooldown_seconds()
    if cooldown <= 0 or not metadata:
        return False
    last = float(metadata.get("last_compact_at", 0) or 0)
    if last <= 0 or time.time() - last >= cooldown:
        return False
    threshold = get_autocompact_threshold(
        model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    )
    hard_limit = threshold + AUTOCOMPACT_BUFFER_TOKENS * 2
    return estimate_message_tokens(messages) < hard_limit


def _mark_compacted(metadata: dict[str, Any] | None) -> None:
    if metadata is not None:
        metadata["last_compact_at"] = time.time()

# ---------------------------------------------------------------------------
# Auto-compact integration (called from query loop)
# ---------------------------------------------------------------------------

async def auto_compact_if_needed(
    messages: list[ConversationMessage],
    *,
    api_client: Any,
    model: str,
    system_prompt: str = "",
    state: AutoCompactState,
    preserve_recent: int = 6,
    progress_callback: CompactProgressCallback | None = None,
    force: bool = False,
    trigger: CompactTrigger = "auto",
    hook_executor: HookExecutor | None = None,
    carryover_metadata: dict[str, Any] | None = None,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
) -> tuple[list[ConversationMessage], bool]:
    """Check if auto-compact should fire, and if so, compact.

    Call this at the start of each query loop turn.

    Returns:
        (messages, was_compacted) — if compacted, messages is the new list.
    """
    if not force and not should_autocompact(
        messages,
        model,
        state,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    ):
        return messages, False

    if not force and _within_compact_cooldown(
        carryover_metadata,
        messages,
        model=model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    ):
        log.info("Skipping auto-compact inside the cache cooldown window")
        return messages, False

    log.info("Auto-compact triggered (failures=%d)", state.consecutive_failures)
    _record_compact_checkpoint(
        carryover_metadata,
        checkpoint=f"query_{trigger}_triggered",
        trigger=trigger,
        message_count=len(messages),
        token_count=estimate_message_tokens(messages),
        details={"consecutive_failures": state.consecutive_failures},
    )

    # Try microcompact first — may be enough
    messages, tokens_freed = microcompact_messages(messages)
    _record_compact_checkpoint(
        carryover_metadata,
        checkpoint="query_microcompact_end",
        trigger=trigger,
        message_count=len(messages),
        token_count=estimate_message_tokens(messages),
        details={"tokens_freed": tokens_freed},
    )
    if tokens_freed > 0 and not should_autocompact(
        messages,
        model,
        state,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    ):
        log.info("Microcompact freed ~%d tokens, auto-compact no longer needed", tokens_freed)
        _mark_compacted(carryover_metadata)
        return messages, True

    context_collapsed = try_context_collapse(messages, preserve_recent=preserve_recent)
    if context_collapsed is not None:
        await _emit_progress(
            progress_callback,
            phase="context_collapse_start",
            trigger=trigger,
            message="Collapsing oversized context before full compaction.",
            checkpoint="query_context_collapse_start",
            metadata=_record_compact_checkpoint(
                carryover_metadata,
                checkpoint="query_context_collapse_start",
                trigger=trigger,
                message_count=len(messages),
                token_count=estimate_message_tokens(messages),
            ),
        )
        messages = context_collapsed
        await _emit_progress(
            progress_callback,
            phase="context_collapse_end",
            trigger=trigger,
            message="Context collapse complete.",
            checkpoint="query_context_collapse_end",
            metadata=_record_compact_checkpoint(
                carryover_metadata,
                checkpoint="query_context_collapse_end",
                trigger=trigger,
                message_count=len(messages),
                token_count=estimate_message_tokens(messages),
            ),
        )
        if not force and not should_autocompact(
            messages,
            model,
            state,
            context_window_tokens=context_window_tokens,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        ):
            _mark_compacted(carryover_metadata)
            return messages, True

    session_memory = try_session_memory_compaction(
        messages,
        preserve_recent=max(preserve_recent, SESSION_MEMORY_KEEP_RECENT),
        trigger=trigger,
        metadata=carryover_metadata,
    )
    if session_memory is not None:
        await _emit_progress(
            progress_callback,
            phase="session_memory_start",
            trigger=trigger,
            message="Condensing earlier conversation into session memory.",
            checkpoint="query_session_memory_start",
            metadata=_record_compact_checkpoint(
                carryover_metadata,
                checkpoint="query_session_memory_start",
                trigger=trigger,
                message_count=len(messages),
                token_count=estimate_message_tokens(messages),
            ),
        )
        await _emit_progress(
            progress_callback,
            phase="session_memory_end",
            trigger=trigger,
            message="Session memory condensation complete.",
            checkpoint="query_session_memory_end",
            metadata=_record_compact_checkpoint(
                carryover_metadata,
                checkpoint="query_session_memory_end",
                trigger=trigger,
                message_count=len(build_post_compact_messages(session_memory)),
                token_count=estimate_message_tokens(build_post_compact_messages(session_memory)),
            ),
        )
        state.compacted = True
        state.turn_counter += 1
        state.turn_id = uuid4().hex
        state.consecutive_failures = 0
        _mark_compacted(carryover_metadata)
        return build_post_compact_messages(session_memory), True

    # Full compact needed
    try:
        result = await compact_conversation(
            messages,
            api_client=api_client,
            model=model,
            system_prompt=system_prompt,
            preserve_recent=preserve_recent,
            suppress_follow_up=True,
            trigger=trigger,
            progress_callback=progress_callback,
            hook_executor=hook_executor,
            carryover_metadata=carryover_metadata,
        )
        state.compacted = True
        state.turn_counter += 1
        state.turn_id = uuid4().hex
        state.consecutive_failures = 0
        _mark_compacted(carryover_metadata)
        return build_post_compact_messages(result), True
    except Exception as exc:  # noqa: BLE001 - compaction failure is recovered by fallback
        state.consecutive_failures += 1
        _record_compact_checkpoint(
            carryover_metadata,
            checkpoint=f"query_{trigger}_failed",
            trigger=trigger,
            message_count=len(messages),
            token_count=estimate_message_tokens(messages),
            details={"reason": str(exc), "consecutive_failures": state.consecutive_failures},
        )
        log.error(
            "Auto-compact failed (attempt %d/%d): %s",
            state.consecutive_failures,
            MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
            exc,
        )
        return messages, False


# ---------------------------------------------------------------------------
# Legacy compat
# ---------------------------------------------------------------------------

def summarize_messages(
    messages: list[ConversationMessage],
    *,
    max_messages: int = 8,
) -> str:
    """Produce a compact textual summary of recent messages (legacy)."""
    selected = messages[-max_messages:]
    lines: list[str] = []
    for message in selected:
        text = message.text.strip()
        if not text:
            continue
        lines.append(f"{message.role}: {text[:300]}")
    return "\n".join(lines)


def compact_messages(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int = 6,
) -> list[ConversationMessage]:
    """Replace older conversation history with a synthetic summary (legacy)."""
    if len(messages) <= preserve_recent:
        return sanitize_conversation_messages(list(messages))
    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)
    summary = summarize_messages(older)
    if not summary:
        return list(newer)
    return sanitize_conversation_messages([
        ConversationMessage(
            role="user",
            content=[TextBlock(text=f"[conversation summary]\n{summary}")],
        ),
        *newer,
    ])
