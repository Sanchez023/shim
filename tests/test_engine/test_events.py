from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.domain.events import AgentEvent, EventSequencer
from openharness.engine.messages import ConversationMessage, ReasoningBlock, TextBlock
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.services.event_log import (
    InMemoryEventLogStore,
    JSONLEventLogStore,
    session_event_log_path,
)
from openharness.services.event_pipeline import create_event_pipeline
from openharness.services.event_runtime import AsyncQueueEventSink, ProjectionRegistry
from openharness.services.projections import AuditState, MetricsState, SessionLogState
from openharness.services.subscribers import (
    JSONLEventSubscriber,
    ProjectionSubscriber,
    UIEventSubscriber,
)
from openharness.tools import create_default_tool_registry


class _OneTurnClient:
    def __init__(self, text: str = "hello", *, reasoning: bool = False) -> None:
        self._text = text
        self._reasoning = reasoning

    async def stream_message(self, request):
        del request
        content = []
        if self._reasoning:
            content.append(
                ReasoningBlock(
                    provider="codex",
                    item_id="rs_1",
                    encrypted_content="opaque",
                    raw={"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
                )
            )
        content.append(TextBlock(text=self._text))
        yield ApiTextDeltaEvent(text=self._text)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=content),
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
        )


def _event(seq: int, kind: str, payload: dict | None = None, **kwargs) -> AgentEvent:
    return AgentEvent(
        event_id=f"e{seq}",
        session_id="s1",
        turn_id="t1",
        step_id="st1",
        seq=seq,
        ts=float(seq),
        kind=kind,
        payload=payload or {},
        **kwargs,
    )


def test_agent_event_round_trip():
    event = _event(7, "user.message", {"message": {"role": "user", "content": []}})

    restored = AgentEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.visibility == "user_visible"


def test_event_sequencer_is_monotonic():
    sequencer = EventSequencer(session_id="s1", start_seq=4)

    first = sequencer.next("turn.start")
    second = sequencer.next("user.message")

    assert first.seq == 5
    assert second.seq == 6
    assert first.session_id == "s1"


def test_session_log_state_replays_messages_and_usage():
    state = SessionLogState()
    state.apply(_event(1, "user.message", {"message": ConversationMessage.from_user_text("hi").model_dump(mode="json")}))
    state.apply(
        _event(
            2,
            "assistant.message",
            {
                "message": ConversationMessage(
                    role="assistant", content=[TextBlock(text="hello")]
                ).model_dump(mode="json"),
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
    )
    state.apply(_event(3, "turn.end", {"status": "completed"}))

    assert [message.text for message in state.derive_messages()] == ["hi", "hello"]
    assert state.derive_usage().total_tokens == 3
    assert state.turn_count == 1


def test_session_log_state_accumulates_cache_usage():
    state = SessionLogState()
    state.apply(
        _event(
            1,
            "assistant.message",
            {
                "message": ConversationMessage(
                    role="assistant", content=[TextBlock(text="hello")]
                ).model_dump(mode="json"),
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 300,
                },
            },
        )
    )

    usage = state.derive_usage()
    assert usage.cache_creation_input_tokens == 100
    assert usage.cache_read_input_tokens == 300
    assert usage.total_tokens == 403


def test_session_log_state_replaces_messages_on_compaction():
    state = SessionLogState()
    state.apply(_event(1, "user.message", {"message": ConversationMessage.from_user_text("old").model_dump(mode="json")}))
    state.apply(
        _event(
            2,
            "context.compacted",
            {"messages": [ConversationMessage.from_user_text("summary").model_dump(mode="json")]},
        )
    )

    assert [message.text for message in state.derive_messages()] == ["summary"]


def test_audit_state_redacts_sensitive_fields():
    state = AuditState()
    state.apply(
        _event(
            1,
            "permission.decided",
            {
                "tool_name": "read_file",
                "dimension": "tool",
                "decision": "deny",
                "reason": "blocked",
                "detail": {
                    "path": "/tmp/x",
                    "api_key": "secret",
                    "nested": {"encrypted_content": "opaque"},
                },
            },
        )
    )

    record = state.snapshot()[0]
    assert record["tool_name"] == "read_file"
    assert record["detail"]["path"] == "/tmp/x"
    assert record["detail"]["api_key"] == "[redacted]"
    assert record["detail"]["nested"]["encrypted_content"] == "[redacted]"


def test_metrics_state_counts_events():
    state = MetricsState()
    state.apply(
        _event(
            1,
            "assistant.message",
            {"message": {}, "usage": {"input_tokens": 5, "output_tokens": 2}},
        )
    )
    state.apply(_event(2, "tool.call", {"tool_name": "glob"}))
    state.apply(_event(3, "tool.result", {"tool_name": "glob", "is_error": True}))
    state.apply(_event(4, "context.compacted", {}))
    state.apply(_event(5, "turn.end", {}))

    snapshot = state.snapshot()
    assert snapshot["total_tokens"] == 7
    assert snapshot["tool_calls"] == 1
    assert snapshot["tool_errors"] == 1
    assert snapshot["compactions"] == 1
    assert snapshot["turns"] == 1


@pytest.mark.asyncio
async def test_jsonl_event_log_round_trip_and_torn_tail(tmp_path: Path):
    path = tmp_path / "session.events.jsonl"
    store = JSONLEventLogStore(path)
    await store.append(_event(1, "user.message", {"message": ConversationMessage.from_user_text("hi").model_dump(mode="json")}))
    await store.append(_event(2, "assistant.message", {"message": ConversationMessage(role="assistant", content=[TextBlock(text="hello")]).model_dump(mode="json"), "usage": {"input_tokens": 1, "output_tokens": 1}}))

    path.write_text(path.read_text(encoding="utf-8") + '{"event_id": "torn"', encoding="utf-8")

    events = await store.read_all()
    messages = await store.derive_messages()

    assert [event.seq for event in events] == [1, 2]
    assert [message.text for message in messages] == ["hi", "hello"]


@pytest.mark.asyncio
async def test_query_engine_writes_replayable_event_log(tmp_path: Path):
    store = InMemoryEventLogStore()
    projections = ProjectionRegistry(
        {"session": SessionLogState(), "audit": AuditState(), "metrics": MetricsState()}
    )
    sink = AsyncQueueEventSink(
        [JSONLEventSubscriber(store), ProjectionSubscriber(projections)]
    )
    sequencer = EventSequencer(session_id="s1")
    engine = QueryEngine(
        api_client=_OneTurnClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=4,
        event_sink=sink,
        event_sequencer=sequencer,
        session_id="s1",
    )

    events = [event async for event in engine.submit_message("hi")]
    await sink.close()

    logged = await store.read_all()
    kinds = [event.kind for event in logged]
    assert kinds[:2] == ["turn.start", "user.message"]
    assert "assistant.message" in kinds
    assert kinds[-1] == "turn.end"
    assert await store.derive_messages() == engine.messages
    assert await store.derive_usage() == UsageSnapshot(input_tokens=3, output_tokens=2)
    assert events


@pytest.mark.asyncio
async def test_reasoning_events_are_session_private(tmp_path: Path):
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    engine = QueryEngine(
        api_client=_OneTurnClient(reasoning=True),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=4,
        event_sink=sink,
        event_sequencer=EventSequencer(session_id="s1"),
        session_id="s1",
    )

    [event async for event in engine.submit_message("hi")]
    logged = await store.read_all()
    assistant = next(event for event in logged if event.kind == "assistant.message")

    assert assistant.visibility == "session_private"
    assert "encrypted_content" in assistant.payload["message"]["content"][0]
    # The private block remains in replayable session state.
    assert "opaque" in (await store.derive_messages())[-1].model_dump_json()


@pytest.mark.asyncio
async def test_ui_subscriber_maps_agent_events():
    subscriber = UIEventSubscriber()
    await subscriber.on_event(
        _event(1, "assistant.text.delta", {"text": "hel"})
    )
    await subscriber.on_event(
        _event(
            2,
            "assistant.message",
            {
                "message": ConversationMessage(
                    role="assistant",
                    content=[
                        ReasoningBlock(provider="codex", raw={"type": "reasoning"}),
                        TextBlock(text="hello"),
                    ],
                ).model_dump(mode="json"),
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )
    )
    await subscriber.close()

    events = [event async for event in subscriber.events()]
    assert [type(event).__name__ for event in events] == [
        "AssistantTextDelta",
        "AssistantTurnComplete",
    ]
    assert events[1].usage.total_tokens == 3
    assert [type(block).__name__ for block in events[1].message.content] == ["TextBlock"]


def test_session_event_log_path_uses_session_id(tmp_path: Path):
    path = session_event_log_path(tmp_path, "abc123")

    assert path == tmp_path / "abc123.events.jsonl"


@pytest.mark.asyncio
async def test_event_pipeline_seeds_legacy_snapshot_once(tmp_path: Path):
    path = tmp_path / "session.events.jsonl"
    pipeline = await create_event_pipeline(tmp_path, "s1", event_log_path=path)
    messages = [
        ConversationMessage.from_user_text("hi"),
        ConversationMessage(role="assistant", content=[TextBlock(text="hello")]),
    ]

    await pipeline.seed_messages(messages)
    await pipeline.seed_messages(messages)
    await pipeline.close()

    store = JSONLEventLogStore(path)
    events = await store.read_all()
    assert [event.kind for event in events] == [
        "user.message",
        "assistant.message",
    ]
    assert all(event.payload.get("restored") is True for event in events)


@pytest.mark.asyncio
async def test_event_pipeline_restores_projections_and_sequence(tmp_path: Path):
    path = tmp_path / "session.events.jsonl"
    store = JSONLEventLogStore(path)
    await store.append(
        _event(
            1,
            "user.message",
            {"message": ConversationMessage.from_user_text("hi").model_dump(mode="json")},
        )
    )
    await store.append(
        _event(
            2,
            "assistant.message",
            {
                "message": ConversationMessage(
                    role="assistant", content=[TextBlock(text="hello")]
                ).model_dump(mode="json"),
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )

    pipeline = await create_event_pipeline(
        tmp_path, "s1", event_log_path=path
    )
    try:
        assert pipeline.sequencer.last_seq == 2
        snapshot = pipeline.projections.snapshot("session")
        assert [message["role"] for message in snapshot["messages"]] == [
            "user",
            "assistant",
        ]
    finally:
        await pipeline.close()


class _HangingClient:
    """Blocks inside the model request so the turn can be interrupted."""

    async def stream_message(self, request):
        del request
        await asyncio.sleep(30)
        if False:  # pragma: no cover - keeps this an async generator
            yield None


@pytest.mark.asyncio
async def test_interrupted_turn_ends_with_interrupted_status(tmp_path: Path):
    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    engine = QueryEngine(
        api_client=_HangingClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=2,
        event_sink=sink,
        event_sequencer=EventSequencer(session_id="s1"),
        session_id="s1",
    )

    async def _drain() -> None:
        async for _ in engine.submit_message("hi"):
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sink.close()

    logged = await store.read_all()
    assert logged[-1].kind == "turn.end"
    assert logged[-1].payload["status"] == "interrupted"
