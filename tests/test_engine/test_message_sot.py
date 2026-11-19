"""Byte-level message SoT gates (ADR-0010 D5 / ADR-0011 I4).

The event log is intended to become the durable source of truth for messages.
These tests compare the provider-visible wire payloads of the live session and
a replay from ``events.jsonl``; semantic equality is not enough because the
cache prefix is byte-based.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest
from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.domain.events import EventSequencer
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolUseBlock,
)
from openharness.engine.cache_diagnostics import _message_wire_payload
from openharness.engine.query_engine import QueryEngine
from openharness.engine.session_manager import SessionManager
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.services.event_log import InMemoryEventLogStore
from openharness.services.event_runtime import AsyncQueueEventSink
from openharness.services.subscribers import JSONLEventSubscriber
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult


class _EmptyInput(BaseModel):
    pass


class _OkTool(BaseTool):
    name = "ok_tool"
    description = "returns ok"
    input_model = _EmptyInput

    def is_read_only(self, arguments: BaseModel) -> bool:
        return True

    async def execute(self, arguments, context) -> ToolResult:
        return ToolResult(output="ok")


class _ToolThenTextClient:
    """One tool call followed by a final text answer."""

    def __init__(self) -> None:
        self.requests = []
        self.calls = 0

    async def stream_message(self, request):
        self.requests.append(
            dataclasses.replace(request, messages=list(request.messages))
        )
        self.calls += 1
        if self.calls == 1:
            message = ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="call_1", name="ok_tool", input={})],
            )
            yield ApiMessageCompleteEvent(
                message=message,
                usage=UsageSnapshot(input_tokens=10, output_tokens=1),
                stop_reason="tool_use",
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text="done")]
            ),
            usage=UsageSnapshot(input_tokens=5, output_tokens=1),
            stop_reason="stop",
        )


def _hashes(messages: list[ConversationMessage]) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(_message_wire_payload(message).encode("utf-8")).hexdigest()
        for message in messages
    )


def _engine(
    tmp_path: Path,
    client,
    store: InMemoryEventLogStore,
    sink: AsyncQueueEventSink,
    *,
    turn_context: str = "",
    coordinator_context: dict[str, str] | None = None,
) -> QueryEngine:
    registry = ToolRegistry()
    registry.register(_OkTool())
    return QueryEngine(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        session_manager=SessionManager(session_id="s1", event_log=store),
        event_sink=sink,
        event_sequencer=EventSequencer(session_id="s1"),
        session_id="s1",
        turn_context_builder=(
            (lambda prompt, metadata: turn_context) if turn_context else None
        ),
        coordinator_context_provider=(
            (lambda: coordinator_context) if coordinator_context is not None else None
        ),
    )


@pytest.mark.asyncio
async def test_replay_wire_hashes_match_live_messages(tmp_path: Path):
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    client = _ToolThenTextClient()
    engine = _engine(
        tmp_path, client, store, sink, turn_context="TURN CONTEXT ONE"
    )

    [event async for event in engine.submit_message("first")]
    assert _hashes(await store.derive_messages()) == _hashes(engine.messages)

    [event async for event in engine.submit_message("second")]
    replayed = await store.derive_messages()
    live = engine.messages

    assert _hashes(replayed) == _hashes(live)
    # The tool result user message is part of both the live history and replay.
    assert any(
        block.type == "tool_result"
        for message in replayed
        for block in message.content
    )


@pytest.mark.asyncio
async def test_coordinator_context_is_request_only(tmp_path: Path):
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    client = _ToolThenTextClient()
    engine = _engine(
        tmp_path,
        client,
        store,
        sink,
        coordinator_context={"workerToolsContext": "TEAM STATE"},
    )

    [event async for event in engine.submit_message("first")]

    first_request = client.requests[0].messages
    assert any(
        block.type == "text"
        and block.text.startswith("# Coordinator User Context")
        for message in first_request
        for block in message.content
        if isinstance(block, TextBlock)
    )

    live = engine.messages
    replayed = await store.derive_messages()
    assert _hashes(replayed) == _hashes(live)
    assert not any(
        message.text.startswith("# Coordinator User Context")
        for message in live
    )
    assert not any(
        message.text.startswith("# Coordinator User Context")
        for message in replayed
    )


@pytest.mark.asyncio
async def test_continue_pending_replay_matches_live(tmp_path: Path):
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    client = _ToolThenTextClient()
    engine = _engine(tmp_path, client, store, sink)

    [event async for event in engine.submit_message("first")]
    [event async for event in engine.continue_pending()]

    assert _hashes(await store.derive_messages()) == _hashes(engine.messages)


@pytest.mark.asyncio
async def test_continue_pending_dangling_tool_use_replay_matches(tmp_path: Path):
    """The replay fold applies the same dangling-tool normalization as resume."""
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    client = _ToolThenTextClient()
    engine = _engine(tmp_path, client, store, sink)

    dangling = [
        ConversationMessage.from_user_text("previous"),
        ConversationMessage(
            role="assistant",
            content=[ToolUseBlock(id="call_missing_output", name="ok_tool", input={})],
        ),
    ]
    engine.load_messages(dangling)
    sequencer = EventSequencer(session_id="s1")
    for message in dangling:
        kind = "user.message" if message.role == "user" else "assistant.message"
        await store.append(
            sequencer.next(kind, {"message": message.model_dump(mode="json")})
        )

    [event async for event in engine.continue_pending()]

    assert _hashes(await store.derive_messages()) == _hashes(engine.messages)
