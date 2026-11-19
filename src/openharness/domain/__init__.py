"""Layer-0 domain types (ADR-0010 D2).

Modules here contain provider-neutral conversation types. They must not import
``openharness.api``, ``openharness.engine``, or ``openharness.services``.
"""

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
from openharness.domain.usage import UsageSnapshot

__all__ = [
    "ContentBlock",
    "ConversationMessage",
    "ImageBlock",
    "ReasoningBlock",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UsageSnapshot",
    "assistant_message_from_api",
    "sanitize_conversation_messages",
    "serialize_anthropic_block",
    "serialize_content_block",
    "strip_private_blocks",
    "visibility_for_message",
]
