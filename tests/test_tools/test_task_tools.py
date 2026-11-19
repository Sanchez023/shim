"""Tests for task and team tools."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from openharness.coordinator.coordinator_mode import get_team_registry
from openharness.tasks import get_task_manager
from openharness.tools.agent_tool import AgentTool, AgentToolInput
from openharness.tools.base import ToolExecutionContext
from openharness.tools.task_create_tool import TaskCreateTool, TaskCreateToolInput
from openharness.tools.task_output_tool import TaskOutputTool, TaskOutputToolInput
from openharness.tools.task_update_tool import TaskUpdateTool, TaskUpdateToolInput
from openharness.tools.team_create_tool import TeamCreateTool, TeamCreateToolInput


async def _wait_for_terminal_task(task_id: str, *, timeout_seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    manager = get_task_manager()
    while asyncio.get_running_loop().time() < deadline:
        task = manager.get_task(task_id)
        if task is not None and task.status in {"completed", "failed", "killed"}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Task {task_id} did not reach a terminal status in time")


@pytest.mark.asyncio
async def test_task_create_and_output_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="echo",
            command="printf 'tool task'",
        ),
        context,
    )
    assert create_result.is_error is False
    task_id = create_result.output.split()[2]

    manager = get_task_manager()
    for _ in range(20):
        if "tool task" in manager.read_task_output(task_id):
            break
        await asyncio.sleep(0.1)
    output_result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id=task_id),
        context,
    )
    assert "tool task" in output_result.output


@pytest.mark.asyncio
async def test_team_create_tool(tmp_path: Path):
    result = await TeamCreateTool().execute(
        TeamCreateToolInput(name="demo", description="test"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert result.is_error is False
    assert "Created team demo" == result.output


@pytest.mark.asyncio
async def test_task_update_tool_updates_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="updatable",
            command="printf 'tool task'",
        ),
        context,
    )
    task_id = create_result.output.split()[2]

    update_result = await TaskUpdateTool().execute(
        TaskUpdateToolInput(
            task_id=task_id,
            progress=60,
            status_note="waiting on verification",
            description="renamed task",
        ),
        context,
    )
    assert update_result.is_error is False

    task = get_task_manager().get_task(task_id)
    assert task is not None
    assert task.description == "renamed task"
    assert task.metadata["progress"] == "60"
    assert task.metadata["status_note"] == "waiting on verification"


def test_agent_tool_defaults_to_worker_profile():
    arguments = AgentToolInput(description="delegate", prompt="do work")

    assert arguments.subagent_type == "worker"


@pytest.mark.asyncio
async def test_agent_tool_uses_subprocess_backend_and_task_is_pollable(
    tmp_path: Path, monkeypatch
):
    """Regression test for #59 / PR #60.

    AgentTool must use the subprocess backend so the returned task_id is
    registered in BackgroundTaskManager and is queryable by the task tools.

    Before the fix, AgentTool hardcoded in_process first.  On macOS/Linux that
    backend is always registered (supports_swarm_mailbox=True), so spawn()
    returned IDs like "in_process_3f7a9b1c2d4e" that BackgroundTaskManager
    never saw — every poll attempt raised ValueError.
    """
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="backend regression check",
            prompt="hello",
            subagent_type="test-worker",
            # command echoes one line and exits — minimal subprocess
            command='python -u -c "import sys; print(sys.stdin.readline().strip())"',
        ),
        context,
    )

    assert not result.is_error, f"AgentTool failed: {result.output}"

    # 1. Backend reported in output must be subprocess, not in_process.
    assert "backend=subprocess" in result.output, (
        f"Expected backend=subprocess in output, got: {result.output}"
    )

    # 2. task_id must NOT be an in-process ID.
    assert "in_process_" not in result.output, (
        f"task_id must not be an in-process ID, got: {result.output}"
    )

    # 3. The task_id must be registered in BackgroundTaskManager so task tools
    #    can query it without raising ValueError.
    #    Parse task_id from "Spawned agent X (task_id=Y, backend=Z)"
    import re
    m = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert m, f"Could not parse task_id from output: {result.output}"
    task_id = m.group(1)

    manager = get_task_manager()
    record = manager.get_task(task_id)
    assert record is not None, (
        f"task_id {task_id!r} not found in BackgroundTaskManager — "
        "task tools (TaskGet, TaskOutput, etc.) would have failed"
    )
    assert record.command == 'python -u -c "import sys; print(sys.stdin.readline().strip())"'
    assert record.type == "local_agent"
    await _wait_for_terminal_task(task_id)


@pytest.mark.asyncio
async def test_send_message_swarm_path_uses_subprocess_backend(
    tmp_path: Path, monkeypatch
):
    """SendMessageTool._send_swarm_message must route via SubprocessBackend.

    Before the fix, _send_swarm_message also hardcoded in_process, so even
    the name@team routing path would fail to find agents spawned by AgentTool.
    """
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    from openharness.tools.send_message_tool import SendMessageTool

    with patch(
        "openharness.swarm.subprocess_backend.SubprocessBackend.send_message",
        new_callable=AsyncMock,
    ) as mock_send:
        await SendMessageTool().execute(
            __import__(
                "openharness.tools.send_message_tool",
                fromlist=["SendMessageToolInput"],
            ).SendMessageToolInput(
                task_id="worker@default",
                message="ping",
            ),
            context,
        )

    # send_message may raise ValueError because no agent was spawned yet
    # (no _agent_tasks entry), but the key assertion is that SubprocessBackend
    # was called — not InProcessBackend.
    mock_send.assert_called_once()
    agent_id_arg = mock_send.call_args[0][0]
    assert agent_id_arg == "worker@default"


@pytest.mark.asyncio
async def test_agent_tool_creates_missing_team_when_team_argument_is_provided(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    get_team_registry()._teams.clear()
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="team auto-create regression",
            prompt="ready",
            subagent_type="test-worker-team",
            team="design-qa-loop",
            command="python -u -c \"import sys; print(sys.stdin.readline().strip())\"",
        ),
        context,
    )

    assert result.is_error is False
    teams = {team.name: team for team in get_team_registry().list_teams()}
    assert "design-qa-loop" in teams
    assert len(teams["design-qa-loop"].agents) == 1


@pytest.mark.asyncio
async def test_agent_tool_supports_remote_and_teammate_modes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    for i, mode in enumerate(("remote_agent", "in_process_teammate")):
        result = await AgentTool().execute(
            AgentToolInput(
                description=f"{mode} smoke",
                prompt="ready",
                mode=mode,
                subagent_type=f"test-worker-{i}",
                command="python -u -c \"import sys; print(sys.stdin.readline().strip())\"",
            ),
            context,
        )
        assert result.is_error is False
        import re

        match = re.search(r"task_id=(\S+?)[,)]", result.output)
        assert match, result.output
        task_id = match.group(1)
        record = get_task_manager().get_task(task_id)
        assert record is not None
        assert record.type == mode
        await _wait_for_terminal_task(task_id)


@pytest.mark.asyncio
async def test_task_output_block_waits_for_completion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="blocking",
            command=(
                'python -u -c "import time; time.sleep(0.4); '
                "print('blocked done')\""
            ),
        ),
        context,
    )
    task_id = create_result.output.split()[2]

    result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id=task_id, block=True, timeout_seconds=10),
        context,
    )

    assert result.is_error is False
    assert "blocked done" in result.output


@pytest.mark.asyncio
async def test_task_output_reports_state_header_for_running_task(tmp_path: Path, monkeypatch):
    """A running task with no output still needs identity and state.

    Regression: the log is empty until the model emits text, so callers saw a
    bare "(no output)" and could not tell which task they were polling or how
    long it had been running.
    """
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="Audit runtime behavior surface vs benchmark coverage",
            command='python -u -c "import time; time.sleep(5)"',
        ),
        context,
    )
    task_id = create_result.output.split()[2]

    try:
        result = await TaskOutputTool().execute(
            TaskOutputToolInput(task_id=task_id),
            context,
        )

        assert result.is_error is False
        assert f"[task {task_id}] local_bash · running" in result.output
        assert "description: Audit runtime behavior surface vs benchmark coverage" in result.output
        assert "running for" in result.output
        assert "no output yet" in result.output
        assert result.metadata["status"] == "running"
        assert result.metadata["description"] == "Audit runtime behavior surface vs benchmark coverage"
    finally:
        await get_task_manager().stop_task(task_id)


@pytest.mark.asyncio
async def test_task_output_notes_truncated_output(tmp_path: Path, monkeypatch):
    """A clipped tail must say so instead of looking like a fresh log."""
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="verbose",
            command='python -u -c "print(\'x\' * 500)"',
        ),
        context,
    )
    task_id = create_result.output.split()[2]

    result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id=task_id, max_bytes=50, block=True, timeout_seconds=10),
        context,
    )

    assert result.is_error is False
    assert "output truncated" in result.output
    assert "showing the last 50" in result.output
    assert result.metadata["truncated"] is True


@pytest.mark.asyncio
async def test_task_output_block_times_out_with_partial_output(monkeypatch):
    from types import SimpleNamespace

    class _RunningManager:
        def get_task(self, task_id: str):
            del task_id
            return SimpleNamespace(status="running")

        def read_task_output(self, task_id: str, max_bytes: int = 12000) -> str:
            del task_id, max_bytes
            return "partial output"

        async def wait_for_task(self, task_id: str, *, timeout_seconds: float):
            del task_id, timeout_seconds
            return SimpleNamespace(status="running")

    monkeypatch.setitem(
        TaskOutputTool.execute.__globals__,
        "get_task_manager",
        lambda: _RunningManager(),
    )

    result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id="task-1", block=True, timeout_seconds=1),
        ToolExecutionContext(cwd=Path(".")),
    )

    assert result.is_error is False
    assert "partial output" in result.output
    assert "still running after 1 seconds" in result.output


@pytest.mark.asyncio
async def test_task_output_block_reports_missing_task(monkeypatch):
    class _MissingManager:
        def get_task(self, task_id: str):
            del task_id

        def read_task_output(self, task_id: str, max_bytes: int = 12000) -> str:
            del task_id, max_bytes
            raise ValueError("Task not found")

        async def wait_for_task(self, task_id: str, *, timeout_seconds: float):
            del task_id, timeout_seconds

    monkeypatch.setitem(
        TaskOutputTool.execute.__globals__,
        "get_task_manager",
        lambda: _MissingManager(),
    )

    result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id="missing", block=True),
        ToolExecutionContext(cwd=Path(".")),
    )

    assert result.is_error is True
    assert "Task not found" in result.output


@pytest.mark.asyncio
async def test_agent_tool_explore_blocks_and_returns_result(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="blocking explore",
            prompt="go",
            subagent_type="Explore",
            command='python -u -c "import sys; sys.stdin.readline(); print(\'explore result\')"',
        ),
        context,
    )

    assert result.is_error is False
    assert "explore result" in result.output
    assert "status=completed" in result.output
    assert result.metadata["status"] == "completed"
    assert result.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_agent_tool_worker_stays_async_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="async worker",
            prompt="go",
            subagent_type="test-worker-async",
            command='python -u -c "import sys; sys.stdin.readline(); print(\'worker result\')"',
        ),
        context,
    )

    assert result.is_error is False
    assert "task_id=" in result.output
    assert result.metadata["blocked"] is False
    match = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert match, result.output
    await _wait_for_terminal_task(match.group(1))


@pytest.mark.asyncio
async def test_agent_tool_explore_can_opt_out_of_waiting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="non-blocking explore",
            prompt="go",
            subagent_type="Explore",
            wait=False,
            command='python -u -c "import sys; sys.stdin.readline(); print(\'explore result\')"',
        ),
        context,
    )

    assert result.is_error is False
    assert result.metadata["blocked"] is False
    match = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert match, result.output
    await _wait_for_terminal_task(match.group(1))


@pytest.mark.asyncio
async def test_agent_tool_explore_reports_timeout_and_keeps_task(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="slow explore",
            prompt="go",
            subagent_type="Explore",
            timeout_seconds=1,
            command=(
                'python -u -c "import sys, time; sys.stdin.readline(); '
                "time.sleep(2); print('late result')\""
            ),
        ),
        context,
    )

    assert result.is_error is False
    assert "still running after 1s" in result.output
    assert result.metadata["status"] == "running"
    match = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert match, result.output
    await _wait_for_terminal_task(match.group(1), timeout_seconds=5.0)
