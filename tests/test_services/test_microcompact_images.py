"""Microcompaction must drop screenshots together with the result they belong to."""

from __future__ import annotations

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.services.compact.constants import TIME_BASED_MC_CLEARED_MESSAGE
from openharness.services.compact.microcompact import microcompact_messages

SCREENSHOT_TOOL = "mcp__playwright__browser_take_screenshot"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUg=="


def _shot_turn(tool_use_id: str) -> list[ConversationMessage]:
    return [
        ConversationMessage(
            role="assistant",
            content=[ToolUseBlock(id=tool_use_id, name=SCREENSHOT_TOOL)],
        ),
        ConversationMessage(
            role="user",
            content=[
                ToolResultBlock(tool_use_id=tool_use_id, content="[1 image(s) returned]"),
                TextBlock(text=f"[image from {SCREENSHOT_TOOL}]"),
                ImageBlock(
                    media_type="image/png",
                    data=PNG_B64,
                    origin_tool_use_id=tool_use_id,
                ),
            ],
        ),
    ]


def test_old_screenshot_is_dropped_with_its_tool_result() -> None:
    messages = [*_shot_turn("toolu_old"), *_shot_turn("toolu_new")]

    microcompact_messages(messages, keep_recent=1)

    old_user = messages[1]
    assert [type(block) for block in old_user.content] == [ToolResultBlock]
    assert old_user.content[0].content == TIME_BASED_MC_CLEARED_MESSAGE

    new_user = messages[3]
    assert any(isinstance(block, ImageBlock) for block in new_user.content), (
        "the most recent screenshot must survive microcompaction"
    )


def test_user_attached_images_are_never_compacted() -> None:
    messages = [
        ConversationMessage(
            role="user",
            content=[
                TextBlock(text="what is this?"),
                ImageBlock(media_type="image/png", data=PNG_B64),
            ],
        ),
        *_shot_turn("toolu_old"),
        *_shot_turn("toolu_new"),
    ]

    microcompact_messages(messages, keep_recent=1)

    assert any(isinstance(block, ImageBlock) for block in messages[0].content)


def test_caption_of_a_kept_image_is_kept() -> None:
    messages = [*_shot_turn("toolu_old")]

    microcompact_messages(messages, keep_recent=1)

    user = messages[1]
    assert [type(block) for block in user.content] == [ToolResultBlock, TextBlock, ImageBlock]
