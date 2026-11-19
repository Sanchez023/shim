"""Backward-compatible re-exports for conversation message models.

The canonical definitions live in :mod:`openharness.domain.messages`
(ADR-0010 D2). New code should import from ``openharness.domain.messages`` so
the API and engine do not depend on each other for layer-0 types.
"""

from __future__ import annotations

from openharness.domain.messages import (
    ContentBlock,
    ConversationMessage,
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message_from_api,
    sanitize_conversation_messages,
    serialize_anthropic_block,
    serialize_content_block,
    strip_private_blocks,
    visibility_for_message,
)

__all__ = [
    "ContentBlock",
    "ConversationMessage",
    "ImageBlock",
    "ReasoningBlock",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "assistant_message_from_api",
    "sanitize_conversation_messages",
    "serialize_anthropic_block",
    "serialize_content_block",
    "strip_private_blocks",
    "visibility_for_message",
]
