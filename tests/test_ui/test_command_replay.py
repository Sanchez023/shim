"""Restored sessions replay user turns as user items, not `> text` system lines."""

from __future__ import annotations

import pytest

from openharness.commands.types import CommandResult
from openharness.domain.messages import ConversationMessage, TextBlock
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
