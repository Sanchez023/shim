from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.domain.events import EventSequencer
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.runtime.registry import CapabilityBundle, CapabilityRegistry
from openharness.runtime.scope import SessionScope
from openharness.runtime.seams.governance.models import SecurityDecision
from openharness.services.event_log import InMemoryEventLogStore
from openharness.services.event_runtime import AsyncQueueEventSink, ProjectionRegistry
from openharness.services.projections import SessionLogState
from openharness.services.subscribers import JSONLEventSubscriber, ProjectionSubscriber
from openharness.tools import BaseTool, ToolResult, create_default_tool_registry


def test_registry_registration_is_reversible():
    registry = CapabilityRegistry()
    first = object()
    second = object()

    dispose_first = registry.register("provider", first)
    dispose_second = registry.register("provider", second, replace=True)

    assert registry.resolve("provider") is second
    dispose_second()
    assert registry.resolve("provider") is first
    dispose_first()
    assert registry.resolve("provider") is None


def test_session_scope_disposes_in_reverse_order():
    registry = CapabilityRegistry()
    disposed: list[str] = []

    with SessionScope(session_id="s1") as scope:
        scope.add_disposer(lambda: disposed.append("first"))
        scope.add_disposer(lambda: disposed.append("second"))
        scope.register(registry, "x", object())

    assert disposed == ["second", "first"]
    assert registry.resolve("x") is None


class _ToolThenTextClient:
    def __init__(self, tool_name: str = "fake") -> None:
        self._tool_name = tool_name
        self.calls = 0

    async def stream_message(self, request):
        del request
        self.calls += 1
        if self.calls == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(id="call_1", name=self._tool_name, input={"x": 1})
                    ],
                ),
                usage=UsageSnapshot(input_tokens=2, output_tokens=1),
                stop_reason="tool_use",
            )
            return
        yield ApiTextDeltaEvent(text="done")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text="done")]),
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
        )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def execute(self, context, tool_name, tool_use_id, tool_input):
        del context
        self.calls.append((tool_name, tool_use_id, tool_input))
        return ToolResultBlock(tool_use_id=tool_use_id, content="tool-ok")


@pytest.mark.asyncio
async def test_query_engine_uses_injected_tool_executor(tmp_path: Path):
    executor = _RecordingExecutor()
    store = InMemoryEventLogStore()
    projections = ProjectionRegistry({"session": SessionLogState()})
    sink = AsyncQueueEventSink(
        [JSONLEventSubscriber(store), ProjectionSubscriber(projections)]
    )
    bundle = CapabilityBundle(
        tool_executor=executor,
        event_sink=sink,
        event_sequencer=EventSequencer(session_id="s1"),
        session_id="s1",
    )
    engine = QueryEngine(
        api_client=_ToolThenTextClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=4,
        capability_bundle=bundle,
    )

    [event async for event in engine.submit_message("go")]

    assert executor.calls == [("fake", "call_1", {"x": 1})]
    events = await store.read_all()
    assert "tool.call" in [event.kind for event in events]
    assert "tool.result" in [event.kind for event in events]
    assert await store.derive_messages() == engine.messages


class _EchoInput(BaseModel):
    x: int = 0


class _EchoTool(BaseTool):
    """A registered tool the fake model can call."""

    name = "echo"
    description = "test tool"
    input_model = _EchoInput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments, context):
        del arguments, context
        self.calls += 1
        return ToolResult(output="ok")


class _RecordingPolicy:
    def __init__(self, decision: SecurityDecision) -> None:
        self.decision = decision
        self.requests: list[Any] = []

    async def evaluate(self, request):
        self.requests.append(request)
        return self.decision


class _RecordingGovernor:
    def __init__(self) -> None:
        self.pre_calls = 0
        self.after_calls = 0

    async def on_step_pre(
        self,
        messages,
        *,
        model,
        context_window_tokens=None,
        auto_compact_threshold_tokens=None,
    ):
        self.pre_calls += 1

    async def on_request_error(self, messages, error):
        return None

    async def after_turn(self, messages, usage):
        self.after_calls += 1


class _RecordingGate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def request(self, *, tool_name, reason, context=None):
        del context
        self.calls.append((tool_name, reason))
        return True


def _engine_with_bundle(tmp_path: Path, bundle: CapabilityBundle) -> tuple[QueryEngine, _EchoTool]:
    echo = _EchoTool()
    registry = create_default_tool_registry()
    registry.register(echo)
    engine = QueryEngine(
        api_client=_ToolThenTextClient("echo"),
        tool_registry=registry,
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=4,
        capability_bundle=bundle,
    )
    return engine, echo


@pytest.mark.asyncio
async def test_query_engine_uses_injected_security_policy(tmp_path: Path):
    policy = _RecordingPolicy(SecurityDecision(allowed=True, dimension="test"))
    engine, echo = _engine_with_bundle(
        tmp_path, CapabilityBundle(security_policy=policy, session_id="s1")
    )

    [event async for event in engine.submit_message("go")]

    assert [request.tool_name for request in policy.requests] == ["echo"]
    assert echo.calls == 1


@pytest.mark.asyncio
async def test_query_engine_uses_injected_context_governor(tmp_path: Path):
    governor = _RecordingGovernor()
    engine, _ = _engine_with_bundle(
        tmp_path, CapabilityBundle(context_governor=governor, session_id="s1")
    )

    [event async for event in engine.submit_message("go")]

    assert governor.pre_calls >= 1
    assert governor.after_calls >= 1


@pytest.mark.asyncio
async def test_query_engine_uses_injected_approval_gate(tmp_path: Path):
    policy = _RecordingPolicy(
        SecurityDecision(
            allowed=False,
            requires_confirmation=True,
            reason="needs approval",
            dimension="test",
        )
    )
    gate = _RecordingGate()
    engine, echo = _engine_with_bundle(
        tmp_path,
        CapabilityBundle(
            security_policy=policy,
            approval_gate=gate,
            session_id="s1",
        ),
    )

    [event async for event in engine.submit_message("go")]

    assert gate.calls == [("echo", "needs approval")]
    assert echo.calls == 1
