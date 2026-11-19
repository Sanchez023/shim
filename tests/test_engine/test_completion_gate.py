"""The completion gate must remind once, then let the turn end."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine import verification_ledger as ledger
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import StatusEvent
from openharness.hooks import HookEvent
from openharness.hooks.types import AggregatedHookResult, HookResult
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.tools import create_default_tool_registry


class RecordingClient:
    """Returns canned assistant texts and remembers every request."""

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.requests: list[object] = []

    async def stream_message(self, request: object):
        self.requests.append(request)
        text = self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=text)]),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


class BlockingStopHooks:
    """Duck-typed hook executor whose STOP hook always blocks."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self.calls: list[HookEvent] = []

    async def execute(self, event: HookEvent, payload: dict) -> AggregatedHookResult:
        self.calls.append(event)
        if event is not HookEvent.STOP:
            return AggregatedHookResult(results=[])
        return AggregatedHookResult(
            results=[HookResult(hook_type="command", success=False, blocked=True, reason=self._reason)]
        )


def build_engine(
    tmp_path: Path,
    client: RecordingClient,
    metadata: dict[str, object],
    hook_executor: object | None = None,
) -> QueryEngine:
    return QueryEngine(
        api_client=client,  # type: ignore[arg-type]
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
        hook_executor=hook_executor,  # type: ignore[arg-type]
        tool_metadata=metadata,
    )


def nudge_notices(events: list[object]) -> list[str]:
    """User-visible notices injected by either gate path."""

    markers = ("Completion gate", "A stop hook blocked")
    return [
        event.message
        for event in events
        if isinstance(event, StatusEvent) and any(m in event.message for m in markers)
    ]


def gate_metadata(**options: object) -> dict[str, object]:
    return {"verification_gate": {"enabled": True, "nudge_limit": 1, **options}}


@pytest.mark.asyncio
async def test_unverified_edit_gets_exactly_one_reminder(tmp_path: Path) -> None:
    metadata = gate_metadata()
    ledger.record_edit(metadata, "src/openharness/engine/query.py")
    ledger.record_run(metadata, command="pytest -q", returncode=0)
    # The green run is now stale: one more edit arrived after it.
    ledger.record_edit(metadata, "src/openharness/engine/step_runner.py")
    client = RecordingClient("Done: refactored the loop.", "Docs-only change; nothing to run.")

    engine = build_engine(tmp_path, client, metadata)
    events = [event async for event in engine.submit_message("refactor the loop")]

    assert len(client.requests) == 2, "the gate must buy exactly one extra model call"
    notices = nudge_notices(events)
    assert len(notices) == 1
    assert "1 edit(s) since the last green run" in notices[0]
    assert ledger.status(metadata).needs_verification is True
    assert metadata["verification_ledger"]["nudges_since_green"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_green_full_run_lets_the_turn_end_without_a_reminder(tmp_path: Path) -> None:
    metadata = gate_metadata()
    ledger.record_edit(metadata, "src/openharness/engine/query.py")
    ledger.record_run(metadata, command="pytest -q", returncode=0)
    client = RecordingClient("Done, full suite green.")

    engine = build_engine(tmp_path, client, metadata)
    events = [event async for event in engine.submit_message("refactor the loop")]

    assert len(client.requests) == 1
    assert nudge_notices(events) == []


@pytest.mark.asyncio
async def test_the_reminder_can_be_disabled(tmp_path: Path) -> None:
    metadata = gate_metadata(nudge_limit=0)
    ledger.record_edit(metadata, "src/openharness/engine/query.py")
    client = RecordingClient("Done.")

    engine = build_engine(tmp_path, client, metadata)
    events = [event async for event in engine.submit_message("refactor the loop")]

    assert len(client.requests) == 1
    assert nudge_notices(events) == []


@pytest.mark.asyncio
async def test_an_unarmed_session_is_never_nagged(tmp_path: Path) -> None:
    metadata = gate_metadata()
    ledger.record_edit(metadata, "docs/adr/0014-completion-gate.md")
    client = RecordingClient("Wrote the ADR.")

    engine = build_engine(tmp_path, client, metadata)
    events = [event async for event in engine.submit_message("write an ADR")]

    assert len(client.requests) == 1
    assert nudge_notices(events) == []


@pytest.mark.asyncio
async def test_a_blocking_stop_hook_holds_the_turn_open_once(tmp_path: Path) -> None:
    metadata = gate_metadata()
    client = RecordingClient("All done.", "Acknowledged: the leftover is intentional.")
    hooks = BlockingStopHooks("tests/test_api is still failing")

    engine = build_engine(tmp_path, client, metadata, hook_executor=hooks)
    events = [event async for event in engine.submit_message("finish up")]

    assert len(client.requests) == 2
    notices = nudge_notices(events)
    assert len(notices) == 1
    assert "tests/test_api is still failing" in notices[0]
    assert hooks.calls.count(HookEvent.STOP) == 2


def dispatch_context(tmp_path: Path, metadata: dict[str, object]):
    """Minimal QueryContext stand-in for exercising the real dispatcher."""

    from types import SimpleNamespace

    return SimpleNamespace(
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        hook_executor=None,
        permission_prompt=None,
        ask_user_prompt=None,
        activate_tools=None,
        approval_gate=None,
        security_policy=None,
        tool_metadata=metadata,
        event_sink=None,
        event_sequencer=None,
        turn_id="turn",
        current_step_id="step",
        tool_executor=None,
    )


@pytest.mark.asyncio
async def test_editing_through_the_dispatcher_arms_the_gate(tmp_path: Path) -> None:
    from openharness.engine.tool_dispatch import execute_tool_call

    metadata = gate_metadata()
    context = dispatch_context(tmp_path, metadata)
    target = tmp_path / "module.py"
    target.write_text("alpha\n", encoding="utf-8")

    result = await execute_tool_call(
        context,
        "write_file",
        "toolu_1",
        {"path": str(target), "content": "beta\n"},
    )

    assert target.read_text(encoding="utf-8") == "beta\n"
    assert ledger.status(metadata).needs_verification is True
    # A tool result is the channel the model reads back, so the status rides on it.
    assert "no workspace-wide run" in result.content


@pytest.mark.asyncio
async def test_a_red_test_run_keeps_the_tool_result_honest(tmp_path: Path) -> None:
    import sys

    from openharness.engine.tool_dispatch import execute_tool_call

    metadata = gate_metadata()
    context = dispatch_context(tmp_path, metadata)
    target = tmp_path / "module.py"
    target.write_text("alpha\n", encoding="utf-8")
    await execute_tool_call(
        context, "write_file", "toolu_1", {"path": str(target), "content": "beta\n"}
    )

    # An empty directory collects no tests, so pytest exits 5: red, not green.
    result = await execute_tool_call(
        context,
        "bash",
        "toolu_2",
        {"command": f"{sys.executable.replace(chr(92), '/')} -m pytest -q"},
    )

    assert ledger.status(metadata).needs_verification is True
    assert "[verification]" in result.content
