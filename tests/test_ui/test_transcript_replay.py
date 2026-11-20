"""Restored sessions replay as the same rows a live turn produces."""

from __future__ import annotations

from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.ui.transcript_replay import transcript_items_for_messages


def _private(text: str) -> TextBlock:
    return TextBlock(text=text, visibility="session_private")


def test_tool_calls_and_results_are_replayed_for_collapsing():
    messages = [
        ConversationMessage(role="user", content=[_private("<turn_x>\ncontext\n</turn_x>")]),
        ConversationMessage.from_user_text("read the readme"),
        ConversationMessage(
            role="assistant",
            content=[
                TextBlock(text="reading it now"),
                ToolUseBlock(id="t1", name="read_file", input={"file_path": "README.md"}),
            ],
        ),
        ConversationMessage(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="hello", is_error=False)],
        ),
        ConversationMessage(role="assistant", content=[TextBlock(text="done")]),
    ]

    items = transcript_items_for_messages(messages)

    assert [(item.role, item.text) for item in items] == [
        ("user", "read the readme"),
        ("assistant", "reading it now"),
        ("tool", 'read_file {"file_path": "README.md"}'),
        ("tool_result", "hello"),
        ("assistant", "done"),
    ]
    assert items[2].tool_name == "read_file"
    assert items[2].tool_use_id == "t1"
    assert items[2].tool_input == {"file_path": "README.md"}
    # The result knows which tool produced it, so the collapsed row can title
    # itself even before the pair is grouped by ``tool_use_id``.
    assert items[3].tool_name == "read_file"
    assert items[3].tool_use_id == "t1"


def test_private_text_blocks_never_reach_the_transcript():
    messages = [
        ConversationMessage(role="user", content=[_private("SECRET CONTEXT")]),
        ConversationMessage(role="user", content=[_private("SECRET CONTEXT"), TextBlock(text="real")]),
    ]

    items = transcript_items_for_messages(messages)

    assert [(item.role, item.text) for item in items] == [("user", "real")]


def test_multi_block_messages_keep_their_structure():
    messages = [
        ConversationMessage(
            role="assistant",
            content=[TextBlock(text="first paragraph"), TextBlock(text="second paragraph")],
        )
    ]

    items = transcript_items_for_messages(messages)

    assert [item.text for item in items] == ["first paragraph", "second paragraph"]


def test_accepts_raw_snapshot_dicts_and_skips_broken_rows():
    raw = [
        {"role": "user", "content": [{"type": "text", "text": "from dict"}]},
        {"role": "assistant", "content": [{"type": "nonsense"}]},
        "not a message",
        {"role": "assistant", "content": [{"type": "text", "text": "still here"}]},
    ]

    items = transcript_items_for_messages(raw)

    assert [item.text for item in items] == ["from dict", "still here"]


def test_empty_text_and_error_flags_are_preserved():
    messages = [
        ConversationMessage(role="assistant", content=[TextBlock(text="   ")]),
        ConversationMessage(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="missing",
                    content="boom",
                    is_error=True,
                    result_metadata={"path": "a.py"},
                )
            ],
        ),
    ]

    [item] = transcript_items_for_messages(messages)

    assert item.role == "tool_result"
    assert item.tool_name is None
    assert item.is_error is True
    assert item.metadata == {"path": "a.py"}
