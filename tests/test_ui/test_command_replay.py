"""Restored sessions replay user turns as user items, not `> text` system lines."""

from __future__ import annotations

import pytest

from openharness.commands.types import CommandResult
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.ui.runtime import _render_command_result


@pytest.mark.asyncio
async def test_replayed_user_messages_render_as_user_items():
    printed: list[tuple[str, str]] = []

    async def print_system(text: str) -> None:
        printed.append(("system", text))

    async def print_user(text: str) -> None:
        printed.append(("user", text))

    async def clear_output() -> None:
        printed.append(("clear", ""))

    result = CommandResult(
        replay_messages=[
            ConversationMessage.from_user_text("earlier question"),
            ConversationMessage(role="assistant", content=[TextBlock(text="earlier answer")]),
            ConversationMessage.from_user_text("   "),
        ]
    )

    events: list[object] = []

    async def render_event(event: object) -> None:
        events.append(event)

    await _render_command_result(
        result, print_system, clear_output, render_event, print_user=print_user
    )

    assert ("clear", "") in printed
    assert ("user", "earlier question") in printed
    assert ("system", "Session restored:") in printed
    # Blank user turns are skipped, and nothing is rendered as "> text".
    assert [kind for kind, _ in printed].count("user") == 1
    assert not any(text.startswith("> ") for _, text in printed)


@pytest.mark.asyncio
async def test_replay_falls_back_to_system_output_without_a_user_printer():
    printed: list[tuple[str, str]] = []

    async def print_system(text: str) -> None:
        printed.append(("system", text))

    async def clear_output() -> None:
        printed.append(("clear", ""))

    async def render_event(event: object) -> None:
        del event

    await _render_command_result(
        CommandResult(replay_messages=[ConversationMessage.from_user_text("legacy")]),
        print_system,
        clear_output,
        render_event,
    )

    assert ("system", "legacy") in printed


@pytest.mark.asyncio
async def test_replay_hides_session_private_turn_context():
    printed: list[tuple[str, str]] = []

    async def print_system(text: str) -> None:
        printed.append(("system", text))

    async def print_user(text: str) -> None:
        printed.append(("user", text))

    async def clear_output() -> None:
        printed.append(("clear", ""))

    events: list[object] = []

    async def render_event(event: object) -> None:
        events.append(event)

    private = TextBlock(
        text="<turn_" + "context>\n# Current Permission Mode\n</turn_" + "context>",
        visibility="session_private",
    )
    await _render_command_result(
        CommandResult(
            replay_messages=[
                ConversationMessage(role="user", content=[private]),
                ConversationMessage.from_user_text("visible question"),
            ]
        ),
        print_system,
        clear_output,
        render_event,
        print_user=print_user,
    )

    user_texts = [text for kind, text in printed if kind == "user"]
    assert user_texts == ["visible question"]
    assert not any("Current Permission Mode" in text for _, text in printed)


@pytest.mark.asyncio
async def test_replay_emits_structured_items_when_a_tui_renderer_is_available():
    printed: list[tuple[str, str]] = []

    async def print_system(text: str) -> None:
        printed.append(("system", text))

    async def clear_output() -> None:
        printed.append(("clear", ""))

    events: list[object] = []

    async def render_event(event: object) -> None:
        events.append(event)

    items: list[object] = []

    async def render_item(item: object) -> None:
        items.append(item)

    private = TextBlock(
        text="<turn_" + "context>\n# Current Permission Mode\n</turn_" + "context>",
        visibility="session_private",
    )
    await _render_command_result(
        CommandResult(
            replay_messages=[
                ConversationMessage(role="user", content=[private]),
                ConversationMessage.from_user_text("list the files"),
                ConversationMessage(
                    role="assistant",
                    content=[
                        TextBlock(text="running ls"),
                        ToolUseBlock(id="tu-1", name="bash", input={"command": "ls"}),
                    ],
                ),
                ConversationMessage(
                    role="user",
                    content=[ToolResultBlock(tool_use_id="tu-1", content="a.py\nb.py")],
                ),
            ]
        ),
        print_system,
        clear_output,
        render_event,
        render_item=render_item,
    )

    assert [(item.role, item.text) for item in items] == [
        ("user", "list the files"),
        ("assistant", "running ls"),
        ("tool", 'bash {"command": "ls"}'),
        ("tool_result", "a.py\nb.py"),
    ]
    # The TUI path does not double-render the same text through the plain
    # user/assistant printers.
    assert events == []
    assert not any(kind == "user" for kind, _ in printed)
