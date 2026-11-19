from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.events import AgentEvent
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.query import QueryContext
from openharness.engine.query_engine import QueryEngine
from openharness.engine.session_manager import SessionManager
from openharness.engine.step_runner import StepRunner
from openharness.engine.stream_events import AssistantTurnComplete
from openharness.engine.turn_executor import TurnExecutor
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.services.event_log import InMemoryEventLogStore
from openharness.tools import create_default_tool_registry


class _ToolThenTextClient:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_message(self, request):
        del request
        self.calls += 1
        if self.calls == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[ToolUseBlock(id="call_1", name="fake", input={"x": 1})],
                ),
                usage=UsageSnapshot(input_tokens=2, output_tokens=1),
                stop_reason="tool_use",
            )
            return
        yield ApiTextDeltaEvent(text="done")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text="done")]
            ),
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
        )


class _TextClient:
    async def stream_message(self, request):
        del request
        yield ApiTextDeltaEvent(text="hello")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text="hello")]
            ),
            usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            stop_reason="end_turn",
        )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def execute(self, context, tool_name, tool_use_id, tool_input):
        del context
        self.calls.append((tool_name, tool_use_id, tool_input))
        return ToolResultBlock(tool_use_id=tool_use_id, content="tool-ok")


def _context(
    tmp_path: Path,
    client,
    *,
    executor=None,
    max_turns: int = 4,
) -> QueryContext:
    return QueryContext(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_tokens=100,
        max_turns=max_turns,
        tool_executor=executor,
        tool_metadata={},
    )


@pytest.mark.asyncio
async def test_session_manager_owns_messages_and_usage():
    session = SessionManager(session_id="s1")
    session.append(ConversationMessage.from_user_text("hi"))
    session.add_usage(UsageSnapshot(input_tokens=2, output_tokens=1))

    assert [message.text for message in session.messages] == ["hi"]
    assert session.total_usage.total_tokens == 3

    session.clear()
    assert session.messages == []
    assert session.total_usage.total_tokens == 0


@pytest.mark.asyncio
async def test_session_manager_restores_from_event_log():
    log = InMemoryEventLogStore()
    await log.append(
        AgentEvent(
            event_id="e1",
            session_id="s1",
            turn_id="t1",
            step_id="",
            seq=1,
            ts=1.0,
            kind="user.message",
            payload={
                "message": ConversationMessage.from_user_text("hi").model_dump(mode="json")
            },
        )
    )
    session = SessionManager(session_id="s1", event_log=log)

    await session.restore_from_event_log()

    assert [message.text for message in session.messages] == ["hi"]


@pytest.mark.asyncio
async def test_step_runner_completes_text_step(tmp_path: Path):
    context = _context(tmp_path, _TextClient())
    runner = StepRunner(context)
    messages = [ConversationMessage.from_user_text("hi")]

    events = [
        event
        async for event in runner.run(
            messages, step_id="step-1", max_tokens=100, turn_count=1
        )
    ]

    assert runner.outcome.status == "completed"
    assert runner.outcome.needs_followup is False
    assert messages[-1].text == "hello"
    assert any(isinstance(event[0], AssistantTurnComplete) for event in events)


@pytest.mark.asyncio
async def test_step_runner_executes_tool_batch_and_requires_followup(tmp_path: Path):
    executor = _RecordingExecutor()
    context = _context(tmp_path, _ToolThenTextClient(), executor=executor)
    runner = StepRunner(context)
    messages = [ConversationMessage.from_user_text("go")]

    [event async for event in runner.run(
        messages, step_id="step-1", max_tokens=100, turn_count=1
    )]

    assert executor.calls == [("fake", "call_1", {"x": 1})]
    assert runner.outcome.needs_followup is True
    assert messages[-1].role == "user"
    assert isinstance(messages[-1].content[0], ToolResultBlock)


@pytest.mark.asyncio
async def test_turn_executor_runs_multiple_steps(tmp_path: Path):
    executor = _RecordingExecutor()
    context = _context(tmp_path, _ToolThenTextClient(), executor=executor)
    messages = [ConversationMessage.from_user_text("go")]

    events = [
        event
        async for event in TurnExecutor(context).execute(messages)
    ]

    assistant_turns = [
        event for event, _ in events if isinstance(event, AssistantTurnComplete)
    ]
    assert len(assistant_turns) == 2
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[-1].text == "done"


@pytest.mark.asyncio
async def test_query_engine_rebuilds_prompt_for_file_mentions(tmp_path: Path):
    calls: list[str] = []

    def builder(prompt: str, metadata: dict[str, object]) -> tuple[str, str]:
        del metadata
        calls.append(prompt)
        return "REBUILT PROMPT", "REBUILT PREFIX"

    engine = QueryEngine(
        api_client=_TextClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="original",
        max_turns=4,
        prompt_builder=builder,
    )

    [event async for event in engine.submit_message("look at @src/app.py")]
    assert engine.system_prompt == "REBUILT PROMPT"
    assert engine.system_prompt_cacheable_prefix == "REBUILT PREFIX"

    [event async for event in engine.submit_message("hello")]
    assert calls == ["look at @src/app.py"]


@pytest.mark.asyncio
async def test_query_engine_exposes_session_manager(tmp_path: Path):
    engine = QueryEngine(
        api_client=_TextClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=4,
    )

    [event async for event in engine.submit_message("hi")]

    assert isinstance(engine.session, SessionManager)
    assert [message.text for message in engine.session.messages] == ["hi", "hello"]
