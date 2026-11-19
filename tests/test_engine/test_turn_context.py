"""Per-turn context is persisted as a private user message, not in the system prompt."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker
from openharness.tools import create_default_tool_registry


@pytest.fixture(autouse=True)
def _clear_coordinator_mode(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


class _CapturingClient:
    def __init__(self) -> None:
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(
            dataclasses.replace(request, messages=list(request.messages))
        )
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text="ok")]
            ),
            usage=UsageSnapshot(input_tokens=10, output_tokens=1),
            stop_reason="stop",
        )


def _engine(tmp_path: Path, client, context: str) -> QueryEngine:
    return QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="STABLE SYSTEM",
        system_prompt_cacheable_prefix="STABLE SYSTEM",
        turn_context_builder=lambda prompt, metadata: context,
    )


def _visible_text(message: ConversationMessage) -> list[str]:
    return [
        block.text
        for block in message.content
        if isinstance(block, TextBlock) and block.visibility == "user_visible"
    ]


def _private_text(messages) -> list[str]:
    return [
        block.text
        for message in messages
        for block in message.content
        if isinstance(block, TextBlock) and block.visibility == "session_private"
    ]


@pytest.mark.asyncio
async def test_turn_context_is_a_private_history_message(tmp_path: Path):
    client = _CapturingClient()
    engine = _engine(tmp_path, client, "TURN CONTEXT")

    [event async for event in engine.submit_message("hello")]

    sent = client.requests[0].messages
    assert len(sent) == 2
    assert sent[0].role == "user"
    assert sent[1].role == "user"
    assert "TURN CONTEXT" in _private_text(sent)[0]
    assert _visible_text(sent[1]) == ["hello"]

    # The real user message stays clean; the context is a separate private
    # message in the persisted history.
    user_messages = [message for message in engine.messages if message.role == "user"]
    assert _visible_text(user_messages[0]) == []
    assert "TURN CONTEXT" in _private_text(user_messages)[0]
    assert _visible_text(user_messages[1]) == ["hello"]


@pytest.mark.asyncio
async def test_turn_context_is_rebuilt_for_each_user_turn(tmp_path: Path):
    client = _CapturingClient()
    contexts = iter(["CONTEXT ONE", "CONTEXT TWO"])
    engine = QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="STABLE SYSTEM",
        system_prompt_cacheable_prefix="STABLE SYSTEM",
        turn_context_builder=lambda prompt, metadata: next(contexts),
    )

    [event async for event in engine.submit_message("first")]
    [event async for event in engine.submit_message("second")]

    first_private = _private_text(client.requests[0].messages)
    second_private = _private_text(client.requests[1].messages)
    assert any("CONTEXT ONE" in text for text in first_private)
    assert any("CONTEXT TWO" in text for text in second_private)
    # The first turn's context is still part of the second request's history,
    # so the persisted prefix can be reused by the provider cache.
    assert any("CONTEXT ONE" in text for text in second_private)
    # The stable system prompt is unchanged across both requests.
    assert client.requests[0].system_prompt == "STABLE SYSTEM"
    assert client.requests[1].system_prompt == "STABLE SYSTEM"
