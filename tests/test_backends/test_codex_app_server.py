from __future__ import annotations

from typing import Any

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.domain.events import EventSequencer
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.session_manager import SessionManager
from openharness.engine.stream_events import AssistantTurnComplete
from openharness.services.event_runtime import AsyncQueueEventSink
from openharness.services.subscribers import JSONLEventSubscriber
from openharness.services.agent_backend import (
    AgentCapabilities,
    LocalPythonBackend,
    SessionRecordingBackend,
)
from openharness.services.backends.codex_app_server import (
    CodexAppServerBackend,
    CodexAppServerClient,
    CodexAppServerSubagentProvider,
)
from openharness.services.event_log import InMemoryEventLogStore
from openharness.services.subagent_provider import SubagentStartRequest


class FakeTransport:
    def __init__(self, notifications: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications_sent: list[tuple[str, dict[str, Any]]] = []
        self.notifications = list(notifications or [])
        self.responses: dict[str, Any] = {
            "initialize": {},
            "thread/start": {"thread": {"id": "thread-1", "ephemeral": True}},
            "turn/start": {"turn": {"id": "turn-1"}},
        }
        self.closed = False

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        del timeout
        self.requests.append((method, params or {}))
        return self.responses[method]

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications_sent.append((method, params or {}))

    async def wait_notification(
        self,
        methods: str | set[str],
        *,
        predicate=None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        del timeout
        wanted = {methods} if isinstance(methods, str) else set(methods)
        for index, message in enumerate(self.notifications):
            if message.get("method") not in wanted:
                continue
            params = message.get("params", {})
            if predicate is not None and not predicate(params):
                continue
            return self.notifications.pop(index)
        raise AssertionError("no matching notification")

    async def close(self) -> None:
        self.closed = True


def _completed_turn(status: str = "completed") -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": status}},
    }


def _agent_message(text: str, phase: str | None) -> dict[str, Any]:
    return {
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"type": "agentMessage", "text": text, "phase": phase},
        },
    }


@pytest.mark.asyncio
async def test_codex_client_runs_one_shot_turn():
    transport = FakeTransport(
        notifications=[
            _agent_message("ignored commentary", "commentary"),
            _agent_message("final answer", "final_answer"),
            _completed_turn(),
        ]
    )
    client = CodexAppServerClient(transport)

    result = await client.run("do the task", cwd="/tmp/project")

    assert result.output == "final answer"
    assert result.stop_reason == "completed"
    assert [method for method, _ in transport.requests] == [
        "initialize",
        "thread/start",
        "turn/start",
    ]
    assert transport.notifications_sent == [("initialized", {})]
    thread_params = transport.requests[1][1]
    assert thread_params["cwd"] == "/tmp/project"
    assert thread_params["ephemeral"] is True
    assert thread_params["approvalPolicy"] == "never"
    turn_params = transport.requests[2][1]
    assert turn_params["threadId"] == "thread-1"
    assert turn_params["input"][0]["text"] == "do the task"


@pytest.mark.asyncio
async def test_codex_client_prefers_final_then_partial_then_unphased():
    transport = FakeTransport(
        notifications=[
            _agent_message("unphased", None),
            _agent_message("partial", "partial_answer"),
            _completed_turn(),
        ]
    )
    client = CodexAppServerClient(transport)

    result = await client.run("do the task", cwd="/tmp")

    assert result.output == "partial"


@pytest.mark.asyncio
async def test_codex_client_maps_permission_modes():
    transport = FakeTransport(notifications=[_agent_message("ok", "final_answer"), _completed_turn()])
    client = CodexAppServerClient(transport, permission_mode="approve-for-me")

    await client.run("task", cwd="/tmp")

    params = transport.requests[1][1]
    assert params["approvalPolicy"] == "on-request"
    assert params["approvalsReviewer"] == "auto_review"
    assert params["sandbox"] == "workspace-write"


@pytest.mark.asyncio
async def test_codex_subagent_provider_normalizes_result():
    def factory(**kwargs):
        return CodexAppServerClient(
            FakeTransport(
                notifications=[
                    _agent_message("delegated answer", "final_answer"),
                    _completed_turn(),
                ]
            ),
            **kwargs,
        )

    provider = CodexAppServerSubagentProvider(client_factory=factory)
    run = await provider.start(SubagentStartRequest(task="delegate", cwd="/tmp"))

    result = await run.wait()
    assert result.output == "delegated answer"
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_codex_agent_backend_emits_turn_complete():
    def factory(**kwargs):
        return CodexAppServerClient(
            FakeTransport(
                notifications=[
                    _agent_message("backend answer", "final_answer"),
                    _completed_turn(),
                ]
            ),
            **kwargs,
        )

    backend = CodexAppServerBackend(cwd="/tmp", client_factory=factory)
    events = [event async for event in backend.submit("do it")]

    assert len(events) == 1
    assert isinstance(events[0], AssistantTurnComplete)
    assert events[0].message.text == "backend answer"


class _FakeEngine:
    def __init__(self) -> None:
        self._messages = []
        self._max_turns = 4

    def set_max_turns(self, value):
        self._max_turns = value

    async def submit_message(self, prompt):
        from openharness.api.usage import UsageSnapshot
        from openharness.engine.messages import ConversationMessage, TextBlock
        from openharness.engine.stream_events import AssistantTurnComplete

        if False:
            yield None
        yield AssistantTurnComplete(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=str(prompt))]),
            usage=UsageSnapshot(),
        )

    async def continue_pending(self, *, max_turns=None):
        del max_turns
        if False:
            yield None


@pytest.mark.asyncio
async def test_session_recording_backend_records_messages_and_events():
    class _ExternalBackend:
        def capabilities(self):
            return AgentCapabilities(
                streaming=False, tools=True, resume=False, cancel=True
            )

        async def submit(self, prompt, *, max_turns=None):
            del prompt, max_turns
            yield AssistantTurnComplete(
                message=ConversationMessage(
                    role="assistant", content=[TextBlock(text="external")]
                ),
                usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            )

        async def resume(self, *, max_turns=None):
            del max_turns
            if False:
                yield None

        async def cancel(self):
            return None

    store = InMemoryEventLogStore()
    sink = AsyncQueueEventSink([JSONLEventSubscriber(store)])
    session = SessionManager(session_id="s1")
    backend = SessionRecordingBackend(
        _ExternalBackend(),
        session,
        event_sink=sink,
        event_sequencer=EventSequencer(session_id="s1"),
        session_id="s1",
    )

    events = [event async for event in backend.submit("hi")]
    await sink.close()

    assert [message.text for message in session.messages] == ["hi", "external"]
    assert [event.kind for event in await store.read_all()] == [
        "turn.start",
        "user.message",
        "assistant.message",
        "turn.end",
    ]
    assert isinstance(events[0], AssistantTurnComplete)


@pytest.mark.asyncio
async def test_local_python_backend_wraps_query_engine():
    backend = LocalPythonBackend(_FakeEngine())
    assert isinstance(backend.capabilities(), AgentCapabilities)

    events = [event async for event in backend.submit("hello")]

    assert isinstance(events[0], AssistantTurnComplete)
