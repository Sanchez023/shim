"""Deterministic session-memory compaction before full LLM compaction."""

from __future__ import annotations

from typing import Any

from openharness.domain.messages import ConversationMessage, ToolResultBlock
from openharness.services.compact.attachments import _build_compact_attachments
from openharness.services.compact.boundaries import (
    _extract_attachment_paths,
    _extract_discovered_tools,
    _finalize_compaction_result,
    _split_preserving_tool_pairs,
    create_compact_boundary_message,
)
from openharness.services.compact.constants import (
    SESSION_MEMORY_KEEP_RECENT,
    SESSION_MEMORY_MAX_CHARS,
    SESSION_MEMORY_MAX_LINES,
    CompactTrigger,
)
from openharness.services.compact.tokens import estimate_message_tokens
from openharness.services.compact.types import CompactionResult


def _summarize_message_for_memory(message: ConversationMessage) -> str:
    text = " ".join(message.text.split())
    if text:
        text = text[:160]
        return f"{message.role}: {text}"
    tool_uses = [block.name for block in message.tool_uses]
    if tool_uses:
        return f"{message.role}: tool calls -> {', '.join(tool_uses[:4])}"
    if any(isinstance(block, ToolResultBlock) for block in message.content):
        return f"{message.role}: tool results returned"
    return f"{message.role}: [non-text content]"


def _build_session_memory_message(messages: list[ConversationMessage]) -> ConversationMessage | None:
    lines: list[str] = []
    total_chars = 0
    for message in messages:
        line = _summarize_message_for_memory(message)
        if not line:
            continue
        projected = total_chars + len(line) + 1
        if lines and (len(lines) >= SESSION_MEMORY_MAX_LINES or projected >= SESSION_MEMORY_MAX_CHARS):
            lines.append("... earlier context condensed ...")
            break
        lines.append(line)
        total_chars = projected
    if not lines:
        return None
    body = "\n".join(lines)
    return ConversationMessage.from_user_text(
        "Session memory summary from earlier in this conversation:\n" + body
    )


def _build_file_session_memory_message(metadata: dict[str, Any] | None) -> ConversationMessage | None:
    """Build a compaction message from the persisted session-memory file."""

    if not metadata:
        return None
    path = metadata.get("session_memory_path")
    if not path:
        return None
    try:
        from openharness.services.session_memory import (
            get_session_memory_content,
            session_memory_to_compact_text,
        )

        text = session_memory_to_compact_text(get_session_memory_content(str(path)))
    except (OSError, ValueError, KeyError):
        return None
    if not text.strip():
        return None
    return ConversationMessage.from_user_text(text)


def try_session_memory_compaction(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int = SESSION_MEMORY_KEEP_RECENT,
    trigger: CompactTrigger = "auto",
    metadata: dict[str, Any] | None = None,
) -> CompactionResult | None:
    """Cheap deterministic compaction for long chats before full LLM compaction."""
    if len(messages) <= preserve_recent + 4:
        return None
    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)
    file_summary_message = _build_file_session_memory_message(metadata)
    summary_message = file_summary_message or _build_session_memory_message(older)
    if summary_message is None:
        return None
    provisional = [summary_message, *newer]
    if (
        estimate_message_tokens(provisional) >= estimate_message_tokens(messages)
        and len(provisional) >= len(messages)
    ):
        return None
    compact_metadata = {
        "trigger": trigger,
        "compact_kind": "session_memory",
        "pre_compact_message_count": len(messages),
        "pre_compact_token_count": estimate_message_tokens(messages),
        "preserve_recent": preserve_recent,
        "used_session_memory": True,
        "used_file_session_memory": file_summary_message is not None,
        "pre_compact_discovered_tools": _extract_discovered_tools(older),
        "attachments": _extract_attachment_paths(older),
    }
    result = CompactionResult(
        trigger=trigger,
        compact_kind="session_memory",
        boundary_marker=create_compact_boundary_message(compact_metadata),
        summary_messages=[summary_message],
        messages_to_keep=list(newer),
        attachments=_build_compact_attachments(older, metadata=metadata),
        hook_results=[],
        compact_metadata=compact_metadata,
    )
    return _finalize_compaction_result(result)
