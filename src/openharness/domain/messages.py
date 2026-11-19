"""Conversation message models shared by the API and the query engine.

These are layer-0 domain types (ADR-0010 D2): provider adapters and the engine
both depend on this module, and neither depends on the other for them.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class TextBlock(BaseModel):
    """Plain text content."""

    type: Literal["text"] = "text"
    text: str
    # Harness-injected context (for example the per-turn context message) is
    # sent to the model but hidden from transcripts and UI projections.
    visibility: Literal["session_private", "user_visible"] = "user_visible"


class ImageBlock(BaseModel):
    """Image content encoded inline for multimodal providers."""

    type: Literal["image"] = "image"
    media_type: str
    data: str
    source_path: str = ""
    #: Set when the image came out of a tool call (for example an MCP
    #: screenshot).  Compaction uses it to drop the image together with the
    #: tool result it belongs to; empty for user-attached images.
    origin_tool_use_id: str = ""

    @classmethod
    def from_path(cls, path: str | Path) -> ImageBlock:
        """Load a local image file into a base64-backed content block."""
        resolved = Path(path).expanduser().resolve()
        media_type, _ = mimetypes.guess_type(str(resolved))
        if not media_type or not media_type.startswith("image/"):
            raise ValueError(f"Unsupported image attachment: {resolved}")
        payload = base64.b64encode(resolved.read_bytes()).decode("ascii")
        return cls(media_type=media_type, data=payload, source_path=str(resolved))


class ToolUseBlock(BaseModel):
    """A request from the model to execute a named tool."""

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(default_factory=lambda: f"toolu_{uuid4().hex}")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    # Provider-native item id (for example Codex ``function_call.id``).  The
    # ``id`` above remains the call id used for tool_result correlation.
    provider_item_id: str = ""


class ToolResultBlock(BaseModel):
    """Tool result content sent back to the model."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False
    result_metadata: dict[str, Any] = Field(default_factory=dict)
    #: Images produced by the tool.  Transport-only: the engine moves them into
    #: the same user message as sibling ``ImageBlock``s (the shape every
    #: provider adapter understands) and clears this field.  Excluded from
    #: serialization so it never reaches a wire payload, a session dump or a
    #: cache fingerprint.
    images: list[ImageBlock] = Field(default_factory=list, exclude=True)


class ThinkingBlock(BaseModel):
    """Anthropic extended-thinking content that must be replayed verbatim.

    ``redacted_thinking`` blocks carry an opaque payload instead of plain
    thinking text; both forms are represented here so a replayed assistant
    message keeps the exact shape the provider returned.
    """

    type: Literal["thinking"] = "thinking"
    visibility: Literal["session_private", "user_visible"] = "session_private"
    thinking: str = ""
    signature: str = ""
    redacted: bool = False
    data: str = ""


class ReasoningBlock(BaseModel):
    """Provider-native reasoning state that must survive a round-trip.

    Codex/Responses providers emit reasoning items alongside assistant output.
    They are not user-visible content, but dropping them breaks cache hits and
    can invalidate encrypted reasoning state on the next request.  ``raw`` keeps
    the original item so it can be replayed without lossy reconstruction.
    """

    type: Literal["reasoning"] = "reasoning"
    visibility: Literal["session_private", "user_visible"] = "session_private"
    provider: str = ""
    text: str = ""
    item_id: str = ""
    summary: list[str] = Field(default_factory=list)
    encrypted_content: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


ContentBlock = Annotated[
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | ReasoningBlock,
    Field(discriminator="type"),
]


class ConversationMessage(BaseModel):
    """A single assistant or user message."""

    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def _normalize_content(cls, value: Any) -> list[Any]:
        """Normalize legacy/null payloads before block validation."""
        if value is None:
            return []
        return cast(list[Any], value)

    @classmethod
    def from_user_text(cls, text: str) -> ConversationMessage:
        """Construct a user message from raw text."""
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def from_user_content(cls, content: list[ContentBlock]) -> ConversationMessage:
        """Construct a user message from explicit content blocks."""
        return cls(role="user", content=list(content))

    @property
    def text(self) -> str:
        """Return concatenated text blocks."""
        return "".join(
            block.text for block in self.content if isinstance(block, TextBlock)
        )

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        """Return all tool calls contained in the message."""
        return [block for block in self.content if isinstance(block, ToolUseBlock)]

    def to_api_param(self, *, include_thinking: bool = True) -> dict[str, Any]:
        """Convert the message into Anthropic SDK message params.

        Reasoning state is provider-specific and cannot be sent to Anthropic;
        the dedicated ``ThinkingBlock`` carries Anthropic thinking instead.
        Set ``include_thinking=False`` when the request has not enabled
        extended thinking.
        """
        content: list[dict[str, Any]] = []
        for block in self.content:
            if isinstance(block, ThinkingBlock) and not include_thinking:
                continue
            param = serialize_anthropic_block(block)
            if param is not None:
                content.append(param)
        return {"role": self.role, "content": content}

    def is_effectively_empty(self) -> bool:
        """Return True when the message carries no useful content."""
        if self.content:
            for block in self.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    return False
                if isinstance(
                    block,
                    (ImageBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock, ReasoningBlock),
                ):
                    return False
        return True


def visibility_for_message(message: ConversationMessage) -> str:
    """Return ``session_private`` when a message carries opaque state."""
    if any(
        getattr(block, "visibility", "user_visible") == "session_private"
        for block in message.content
    ):
        return "session_private"
    return "user_visible"


def strip_private_blocks(message: ConversationMessage) -> ConversationMessage:
    """Return a copy safe for UI, audit and telemetry projections.

    Provider-opaque reasoning remains in the session-private event log; only
    user-visible blocks cross the projection boundary.
    """
    return ConversationMessage(
        role=message.role,
        content=[
            block
            for block in message.content
            if getattr(block, "visibility", "user_visible") == "user_visible"
        ],
    )


def sanitize_conversation_messages(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Normalize restored conversation history into a provider-safe sequence.

    This drops legacy empty assistant messages and trims malformed trailing tool
    turns, such as an assistant ``tool_use`` message that never received a
    matching user ``tool_result`` response. Those broken tails can happen when a
    session is interrupted mid-turn and would later cause OpenAI-compatible
    providers to reject the resumed conversation.
    """
    sanitized: list[ConversationMessage] = []
    pending_tool_use_ids: set[str] = set()
    pending_tool_use_index: int | None = None

    for message in messages:
        if message.role == "assistant" and message.is_effectively_empty():
            continue

        tool_uses = message.tool_uses if message.role == "assistant" else []
        tool_results = [
            block for block in message.content if isinstance(block, ToolResultBlock)
        ] if message.role == "user" else []

        matched_pending_tool_results = False
        if pending_tool_use_ids:
            result_ids = {block.tool_use_id for block in tool_results}
            if message.role != "user" or not pending_tool_use_ids.issubset(result_ids):
                if pending_tool_use_index is not None and pending_tool_use_index < len(sanitized):
                    sanitized.pop(pending_tool_use_index)
                pending_tool_use_ids = set()
                pending_tool_use_index = None
            else:
                matched_pending_tool_results = True
                pending_tool_use_ids = set()
                pending_tool_use_index = None

        if message.role == "user" and tool_results and not matched_pending_tool_results:
            content: list[ContentBlock] = [
                block for block in message.content if not isinstance(block, ToolResultBlock)
            ]
            if not content:
                continue
            message = ConversationMessage(role="user", content=content)

        sanitized.append(message)

        if tool_uses:
            pending_tool_use_ids = {block.id for block in tool_uses}
            pending_tool_use_index = len(sanitized) - 1

    if pending_tool_use_ids and pending_tool_use_index is not None and pending_tool_use_index < len(sanitized):
        sanitized.pop(pending_tool_use_index)

    return sanitized


def serialize_anthropic_block(block: ContentBlock) -> dict[str, Any] | None:
    """Convert a local content block into the Anthropic wire format.

    Returns ``None`` for blocks that are meaningful only to another provider
    (for example Codex reasoning items), so callers can omit them.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": block.data,
            },
        }

    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }

    if isinstance(block, ThinkingBlock):
        if block.redacted:
            return {"type": "redacted_thinking", "data": block.data}
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }

    if isinstance(block, ReasoningBlock):
        return None

    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }


def serialize_content_block(block: ContentBlock) -> dict[str, Any]:
    """Backward-compatible Anthropic serializer.

    Kept for callers that expect a concrete dict.  Provider-specific blocks
    that cannot be sent to Anthropic raise ``ValueError``; use
    :func:`serialize_anthropic_block` when omission is desired.
    """
    param = serialize_anthropic_block(block)
    if param is None:
        raise ValueError(
            f"{type(block).__name__} cannot be serialized for the Anthropic API"
        )
    return param


def assistant_message_from_api(raw_message: Any) -> ConversationMessage:
    """Convert an Anthropic SDK message object into a conversation message."""
    content: list[ContentBlock] = []

    for raw_block in getattr(raw_message, "content", []):
        block_type = getattr(raw_block, "type", None)
        if block_type == "text":
            content.append(TextBlock(text=getattr(raw_block, "text", "")))
        elif block_type == "thinking":
            content.append(
                ThinkingBlock(
                    thinking=getattr(raw_block, "thinking", ""),
                    signature=getattr(raw_block, "signature", ""),
                )
            )
        elif block_type == "redacted_thinking":
            content.append(
                ThinkingBlock(
                    redacted=True,
                    data=getattr(raw_block, "data", ""),
                )
            )
        elif block_type == "tool_use":
            content.append(
                ToolUseBlock(
                    id=getattr(raw_block, "id", f"toolu_{uuid4().hex}"),
                    name=getattr(raw_block, "name", ""),
                    input=dict(getattr(raw_block, "input", {}) or {}),
                )
            )

    return ConversationMessage(role="assistant", content=content)
