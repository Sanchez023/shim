"""Rebuild TUI transcript rows from persisted conversation messages.

A resumed session used to be replayed as one flat text row per message.  That
loses everything the live transcript renders structurally: tool calls and their
results (which the TUI collapses into a single ``⏺ tool(args)`` row), the
paragraph break between the text blocks around them, and — worst — the
``session_private`` per-turn context message, which surfaced as a giant user
bubble.

This module turns persisted messages back into the exact item shapes the live
stream emits, so a restored conversation looks like a live one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.ui.protocol import TranscriptItem

log = logging.getLogger(__name__)


def _parse_messages(raw_messages: list[Any]) -> list[ConversationMessage]:
    parsed: list[ConversationMessage] = []
    for raw in raw_messages:
        if isinstance(raw, ConversationMessage):
            parsed.append(raw)
            continue
        try:
            parsed.append(ConversationMessage.model_validate(raw))
        except Exception:  # one bad row must not kill the replay
            # Logged rather than swallowed: a systematically broken snapshot
            # would otherwise restore as a silently truncated conversation.
            log.debug("skipping unrestorable transcript row", exc_info=True)
    return parsed


def transcript_items_for_messages(raw_messages: list[Any]) -> list[TranscriptItem]:
    """Return transcript rows for a restored conversation, in message order.

    ``session_private`` text blocks (harness context) are dropped. Tool calls
    and tool results are re-emitted with the same fields the live stream uses,
    so ``groupToolPairs`` collapses them and ``ctrl+o`` can expand them.
    """
    messages = _parse_messages(raw_messages)
    tool_names: dict[str, str] = {
        block.id: block.name
        for message in messages
        for block in message.content
        if isinstance(block, ToolUseBlock)
    }

    items: list[TranscriptItem] = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.visibility != "user_visible":
                    continue
                text = block.text.strip()
                if text:
                    items.append(TranscriptItem(role=message.role, text=text))
            elif isinstance(block, ToolUseBlock):
                items.append(
                    TranscriptItem(
                        role="tool",
                        text=f"{block.name} {json.dumps(block.input, ensure_ascii=True)}",
                        tool_name=block.name,
                        tool_use_id=block.id,
                        tool_input=block.input,
                    )
                )
            elif isinstance(block, ToolResultBlock):
                items.append(
                    TranscriptItem(
                        role="tool_result",
                        text=block.content,
                        tool_name=tool_names.get(block.tool_use_id),
                        tool_use_id=block.tool_use_id,
                        is_error=block.is_error,
                        metadata=block.result_metadata or None,
                    )
                )
    return items
