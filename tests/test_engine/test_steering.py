"""Follow-up prompts typed mid-turn reach the model at the next step boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings, Settings
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.query_engine import QueryEngine
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult


class _NoInput(BaseModel):
    pass


class _QueueingTool(BaseTool):
    """Queues a follow-up while it runs, exactly like the TUI does mid-turn."""

    name = "QueueProbe"
    description = "queue a follow-up"
    input_model = _NoInput

    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder

    def is_read_only(self, arguments) -> bool:
        del arguments
        return True

    async def execute(self, arguments, context) -> ToolResult:
        del arguments, context
        engine = self._holder["engine"]
        self._holder["queued"] = engine.queue_user_message(
            ConversationMessage.from_user_text("steer me")
        )
        return ToolResult(output="probe done")


class _ScriptedClient:
    """Answer with a tool call, then with plain text."""

    base_url = "https://example.test/v1"

    def __init__(self, tool_name: str = "QueueProbe") -> None:
        self.requests: list[Any] = []
        self._tool_name = tool_name

    async def stream_message(self, request):
        import dataclasses

        self.requests.append(
            dataclasses.replace(
                request,
                messages=[message.model_copy(deep=True) for message in request.messages],
            )
        )
        if len(self.requests) == 1:
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(id="tool-1", name=self._tool_name, input={}),
                ],
            )
        else:
            message = ConversationMessage(
                role="assistant", content=[TextBlock(text="done")]
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=5, output_tokens=1),
            stop_reason="stop",
        )


class _BackgroundProbeTool(BaseTool):
    """Asks the engine to background itself, like Ctrl+B does."""

    name = "BackgroundProbe"
    description = "background probe"
    input_model = _NoInput

    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder

    def is_read_only(self, arguments) -> bool:
        del arguments
        return True

    async def execute(self, arguments, context) -> ToolResult:
        del arguments
        engine = self._holder["engine"]
        self._holder["has_event"] = context.background_event is not None
        self._holder["requested"] = engine.request_background()
        await asyncio.sleep(0)
        self._holder["event_set"] = (
            context.background_event is not None and context.background_event.is_set()
        )
        return ToolResult(output="probe done")


def _build_engine(cwd: Path, client: _ScriptedClient, holder: dict[str, Any]) -> QueryEngine:
    registry = ToolRegistry()
    registry.register(_QueueingTool(holder))
    registry.register(_BackgroundProbeTool(holder))
    return QueryEngine(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=cwd,
        model="steering-model",
        system_prompt="SYSTEM",
        settings=Settings(),
        warmup_wait_seconds=0.0,
    )


async def _drain(engine: QueryEngine, prompt: str) -> None:
    async for _event in engine.submit_message(prompt):
        pass


@pytest.mark.asyncio
async def test_queued_message_is_injected_at_the_next_step(tmp_path):
    client = _ScriptedClient()
    holder: dict[str, Any] = {}
    engine = _build_engine(tmp_path, client, holder)
    holder["engine"] = engine

    await _drain(engine, "start")

    assert holder["queued"] is True
    assert len(client.requests) == 2, "expected the tool step and the follow-up step"

    second_step = client.requests[1].messages
    # The follow-up lands after the tool result, as the newest message, so the
    # next model request sees it (and the tool_use/tool_result pair stays intact).
    assert second_step[-1].role == "user"
    assert second_step[-1].text == "steer me"
    tool_results = [
        block
        for message in second_step
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert any("probe done" in block.content for block in tool_results)
    assert any(message.text == "steer me" for message in engine.messages)


@pytest.mark.asyncio
async def test_queue_user_message_rejects_calls_outside_a_turn(tmp_path):
    engine = _build_engine(tmp_path, _ScriptedClient(), {})

    queued = engine.queue_user_message(ConversationMessage.from_user_text("too early"))

    assert queued is False
    assert engine.messages == []


@pytest.mark.asyncio
async def test_request_background_reports_nothing_to_background(tmp_path):
    engine = _build_engine(tmp_path, _ScriptedClient(), {})

    assert engine.request_background() is False


@pytest.mark.asyncio
async def test_request_background_sets_the_running_tool_event(tmp_path):
    engine = _build_engine(tmp_path, _ScriptedClient(), {})
    event = asyncio.Event()
    context = engine._build_query_context(turn_id="turn", max_turns=1)
    context.active_tool_backgrounds.add(event)
    engine._active_context = context

    assert engine.request_background() is True
    assert event.is_set() is True


@pytest.mark.asyncio
async def test_running_tools_receive_a_background_event(tmp_path):
    """The dispatcher hands every tool call an event Ctrl+B can set."""
    client = _ScriptedClient(tool_name="BackgroundProbe")
    holder: dict[str, Any] = {}
    engine = _build_engine(tmp_path, client, holder)
    holder["engine"] = engine

    await _drain(engine, "start")

    assert holder["has_event"] is True
    assert holder["requested"] is True
    assert holder["event_set"] is True
