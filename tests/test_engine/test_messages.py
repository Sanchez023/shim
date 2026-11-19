from __future__ import annotations

import pytest

from openharness.engine.messages import (
    ConversationMessage,
    ReasoningBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_conversation_messages,
    serialize_anthropic_block,
    serialize_content_block,
    strip_private_blocks,
)


def test_sanitize_conversation_messages_keeps_complete_tool_turn():
    messages = [
        ConversationMessage.from_user_text("edit the file"),
        ConversationMessage(
            role="assistant",
            content=[ToolUseBlock(id="write_file:234", name="write_file", input={"path": "x"})],
        ),
        ConversationMessage(
            role="user",
            content=[ToolResultBlock(tool_use_id="write_file:234", content="ok", is_error=False)],
        ),
    ]

    sanitized = sanitize_conversation_messages(messages)

    assert sanitized == messages


def test_sanitize_conversation_messages_drops_dangling_trailing_tool_use():
    messages = [
        ConversationMessage.from_user_text("edit the file"),
        ConversationMessage(
            role="assistant",
            content=[ToolUseBlock(id="write_file:234", name="write_file", input={"path": "x"})],
        ),
    ]

    sanitized = sanitize_conversation_messages(messages)

    assert sanitized == [ConversationMessage.from_user_text("edit the file")]


def test_sanitize_conversation_messages_drops_orphan_tool_results_but_keeps_user_text():
    messages = [
        ConversationMessage.from_user_text("hello"),
        ConversationMessage(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="missing_call", content="stale", is_error=True),
                TextBlock(text="new prompt"),
            ],
        ),
    ]

    sanitized = sanitize_conversation_messages(messages)

    assert sanitized == [
        ConversationMessage.from_user_text("hello"),
        ConversationMessage(role="user", content=[TextBlock(text="new prompt")]),
    ]


def test_serialize_content_block_keeps_legacy_signature():
    assert serialize_content_block(TextBlock(text="hello")) == {
        "type": "text",
        "text": "hello",
    }


def test_reasoning_block_is_omitted_from_anthropic_params():
    message = ConversationMessage(
        role="assistant",
        content=[
            ReasoningBlock(provider="codex", raw={"type": "reasoning", "id": "rs_1"}),
            TextBlock(text="visible"),
        ],
    )

    assert message.to_api_param()["content"] == [{"type": "text", "text": "visible"}]
    with pytest.raises(ValueError):
        serialize_content_block(ReasoningBlock(provider="codex"))


def test_thinking_block_serializes_for_anthropic_and_is_private():
    block = ThinkingBlock(thinking="reasoning", signature="sig")

    assert block.visibility == "session_private"
    assert serialize_anthropic_block(block) == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "sig",
    }


def test_reasoning_block_defaults_to_session_private():
    block = ReasoningBlock(provider="codex")

    assert block.visibility == "session_private"


def test_to_api_param_can_strip_thinking():
    message = ConversationMessage(
        role="assistant",
        content=[ThinkingBlock(thinking="private", signature="sig"), TextBlock(text="hi")],
    )

    payload = message.to_api_param(include_thinking=False)

    assert payload["content"] == [{"type": "text", "text": "hi"}]


def test_strip_private_blocks_keeps_only_visible_content():
    message = ConversationMessage(
        role="assistant",
        content=[
            ReasoningBlock(provider="codex", raw={"type": "reasoning", "id": "rs_1"}),
            ThinkingBlock(thinking="private", signature="sig"),
            TextBlock(text="visible"),
        ],
    )

    public = strip_private_blocks(message)

    assert len(public.content) == 1
    assert isinstance(public.content[0], TextBlock)
    assert public.content[0].text == "visible"
