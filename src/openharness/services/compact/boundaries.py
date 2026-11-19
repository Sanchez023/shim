"""Compaction boundaries and message-segment helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_conversation_messages,
)
from openharness.services.compact.constants import (
    MAX_COMPACT_ATTACHMENTS,
    MAX_DISCOVERED_TOOLS,
    CompactionKind,
    CompactTrigger,
)
from openharness.services.compact.tokens import estimate_message_tokens
from openharness.services.compact.types import CompactAttachment, CompactionResult


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)

def _extract_attachment_paths(messages: list[ConversationMessage]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    path_pattern = re.compile(r"path:\s*([^)\\n]+)")
    attachment_pattern = re.compile(r"\[attachment:\s*([^\]]+)\]")
    for message in messages:
        for block in message.content:
            if isinstance(block, ImageBlock) and block.source_path:
                path = str(Path(block.source_path).expanduser())
                if path not in seen:
                    seen.add(path)
                    found.append(path)
            elif isinstance(block, TextBlock):
                for match in path_pattern.findall(block.text):
                    path = match.strip()
                    if path and path not in seen:
                        seen.add(path)
                        found.append(path)
                for match in attachment_pattern.findall(block.text):
                    path = match.strip()
                    if path and "download failed" not in path and path not in seen:
                        seen.add(path)
                        found.append(path)
            if len(found) >= MAX_COMPACT_ATTACHMENTS:
                return found
    return found


def _extract_discovered_tools(messages: list[ConversationMessage]) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for tool_use in message.tool_uses:
            if tool_use.name and tool_use.name not in seen:
                seen.add(tool_use.name)
                discovered.append(tool_use.name)
            if len(discovered) >= MAX_DISCOVERED_TOOLS:
                return discovered
    return discovered

def render_compact_attachment(attachment: CompactAttachment) -> ConversationMessage:
    """Serialize a structured compact attachment into a conversation message."""
    header = f"[Compact attachment: {attachment.kind}] {attachment.title}".strip()
    text = f"{header}\n{attachment.body}".strip()
    return ConversationMessage.from_user_text(text)


def create_compact_boundary_message(metadata: dict[str, Any]) -> ConversationMessage:
    """Create a boundary marker message for post-compact conversation rebuild."""
    lines = [
        "[Compact boundary marker]",
        "Earlier conversation was compacted. Use the summary and preserved assets below as the continuity boundary.",
    ]
    trigger = str(metadata.get("trigger") or "").strip()
    compact_kind = str(metadata.get("compact_kind") or "").strip()
    pre_messages = metadata.get("pre_compact_message_count")
    pre_tokens = metadata.get("pre_compact_token_count")
    post_messages = metadata.get("post_compact_message_count")
    post_tokens = metadata.get("post_compact_token_count")
    if trigger:
        lines.append(f"Trigger: {trigger}")
    if compact_kind:
        lines.append(f"Compaction kind: {compact_kind}")
    if pre_messages is not None or pre_tokens is not None:
        lines.append(
            "Pre-compact footprint: "
            f"messages={pre_messages if pre_messages is not None else 'unknown'}, "
            f"tokens={pre_tokens if pre_tokens is not None else 'unknown'}"
        )
    if post_messages is not None or post_tokens is not None:
        lines.append(
            "Post-compact footprint: "
            f"messages={post_messages if post_messages is not None else 'unknown'}, "
            f"tokens={post_tokens if post_tokens is not None else 'unknown'}"
        )
    anchor = str(metadata.get("preserved_segment_anchor") or "").strip()
    if anchor:
        lines.append(f"Preserved segment anchor: {anchor}")
    return ConversationMessage.from_user_text("\n".join(lines))


def build_post_compact_messages(result: CompactionResult) -> list[ConversationMessage]:
    """Rebuild the post-compact message list in Claude Code's ordering."""
    attachment_messages = [render_compact_attachment(attachment) for attachment in result.attachments]
    hook_messages = [render_compact_attachment(attachment) for attachment in result.hook_results]
    return [
        result.boundary_marker,
        *result.summary_messages,
        *result.messages_to_keep,
        *attachment_messages,
        *hook_messages,
    ]

def _boundary_crosses_tool_pair(previous: ConversationMessage, current: ConversationMessage) -> bool:
    """Return True when a preserve boundary would split a tool_use/result pair."""

    if previous.role != "assistant" or current.role != "user":
        return False
    pending_tool_ids = {block.id for block in previous.content if isinstance(block, ToolUseBlock)}
    if not pending_tool_ids:
        return False
    result_ids = {block.tool_use_id for block in current.content if isinstance(block, ToolResultBlock)}
    return bool(pending_tool_ids & result_ids)


def _split_preserving_tool_pairs(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int,
) -> tuple[list[ConversationMessage], list[ConversationMessage]]:
    """Split older/newer segments without cutting through a tool_use/result pair.

    The preserved segment is also sanitized so trailing orphan tool_use blocks
    never survive the compaction boundary.
    """

    if len(messages) <= preserve_recent:
        return [], sanitize_conversation_messages(list(messages))

    split_index = max(0, len(messages) - preserve_recent)
    while split_index > 0 and _boundary_crosses_tool_pair(messages[split_index - 1], messages[split_index]):
        split_index -= 1

    older = list(messages[:split_index])
    newer = sanitize_conversation_messages(list(messages[split_index:]))
    return older, newer


def _sanitize_compaction_segments(result: CompactionResult) -> None:
    """Normalize summary+preserved messages into a provider-safe sequence."""

    if not result.summary_messages and not result.messages_to_keep:
        return
    combined = [*result.summary_messages, *result.messages_to_keep]
    sanitized = sanitize_conversation_messages(combined)
    summary_count = len(result.summary_messages)
    result.summary_messages = sanitized[:summary_count]
    result.messages_to_keep = sanitized[summary_count:]

def _finalize_compaction_result(result: CompactionResult) -> CompactionResult:
    _sanitize_compaction_segments(result)
    messages = build_post_compact_messages(result)
    result.compact_metadata.setdefault("post_compact_message_count", len(messages))
    result.compact_metadata.setdefault("post_compact_token_count", estimate_message_tokens(messages))
    result.boundary_marker = create_compact_boundary_message(result.compact_metadata)
    return result


def _metadata_has_checkpoint(metadata: dict[str, Any] | None, checkpoint: str) -> bool:
    if metadata is None:
        return False
    checkpoints = metadata.get("compact_checkpoints")
    if not isinstance(checkpoints, list):
        return False
    return any(isinstance(entry, dict) and entry.get("checkpoint") == checkpoint for entry in checkpoints)


def _build_passthrough_compaction_result(
    messages: list[ConversationMessage],
    *,
    trigger: CompactTrigger,
    compact_kind: CompactionKind,
    metadata: dict[str, Any] | None = None,
) -> CompactionResult:
    compact_metadata: dict[str, Any] = {
        "trigger": trigger,
        "compact_kind": compact_kind,
        "pre_compact_message_count": len(messages),
        "pre_compact_token_count": estimate_message_tokens(messages),
        **_sanitize_metadata(metadata or {}),
    }
    result = CompactionResult(
        trigger=trigger,
        compact_kind=compact_kind,
        boundary_marker=create_compact_boundary_message(compact_metadata),
        summary_messages=[],
        messages_to_keep=list(messages),
        attachments=[],
        hook_results=[],
        compact_metadata=compact_metadata,
    )
    return _finalize_compaction_result(result)
