"""Plan-mode switch tools must update the live runtime, not only disk."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.config.settings import PermissionSettings
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.tools import create_default_tool_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.enter_plan_mode_tool import (
    EnterPlanModeTool,
    EnterPlanModeToolInput,
)
from openharness.tools.exit_plan_mode_tool import (
    ExitPlanModeTool,
    ExitPlanModeToolInput,
)


@pytest.mark.asyncio
async def test_exit_plan_mode_calls_runtime_callback(tmp_path: Path) -> None:
    calls: list[str] = []

    result = await ExitPlanModeTool().execute(
        ExitPlanModeToolInput(),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={"set_permission_mode": calls.append},
        ),
    )

    assert calls == ["full_auto"]
    assert "auto" in result.output


@pytest.mark.asyncio
async def test_enter_plan_mode_calls_runtime_callback(tmp_path: Path) -> None:
    calls: list[str] = []

    result = await EnterPlanModeTool().execute(
        EnterPlanModeToolInput(),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={"set_permission_mode": calls.append},
        ),
    )

    assert calls == ["plan"]
    assert "plan" in result.output


def test_query_engine_set_permission_mode_updates_live_checker(tmp_path: Path) -> None:
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))
    engine = QueryEngine(
        api_client=object(),  # not used by this test
        tool_registry=create_default_tool_registry(),
        permission_checker=checker,
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
    )

    blocked = checker.evaluate("write_file", is_read_only=False)
    assert blocked.allowed is False
    assert "plan mode" in blocked.reason

    engine.set_permission_mode(PermissionMode.FULL_AUTO)

    decision = checker.evaluate("write_file", is_read_only=False)
    assert decision.allowed is True


def test_explore_and_plan_agents_are_read_only():
    from openharness.tools.agent_tool import AgentTool, AgentToolInput

    tool = AgentTool()
    assert tool.is_read_only(AgentToolInput(description="scan", prompt="x", subagent_type="Explore"))
    assert tool.is_read_only(AgentToolInput(description="design", prompt="x", subagent_type="Plan"))
    assert not tool.is_read_only(AgentToolInput(description="build", prompt="x", subagent_type="worker"))
    assert not tool.is_read_only(
        AgentToolInput(description="verify", prompt="x", subagent_type="verification")
    )


@pytest.mark.asyncio
async def test_todo_write_uses_the_session_plan_in_plan_mode(tmp_path: Path) -> None:
    from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"permission_mode": "plan", "plan_file": str(plan_file)},
    )

    result = await TodoWriteTool().execute(
        TodoWriteToolInput(item="inspect the seams", path="TODO.md"),
        context,
    )

    assert result.is_error is False
    assert result.metadata["path"] == str(plan_file)
    assert "- [ ] inspect the seams" in plan_file.read_text(encoding="utf-8")
    assert not (tmp_path / "TODO.md").exists()


@pytest.mark.asyncio
async def test_todo_write_defaults_to_the_session_checklist_outside_plan_mode(
    tmp_path: Path,
) -> None:
    from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"permission_mode": "full_auto", "plan_file": str(plan_file)},
    )

    await TodoWriteTool().execute(TodoWriteToolInput(item="project task"), context)

    assert "- [ ] project task" in plan_file.read_text(encoding="utf-8")
    assert not (tmp_path / "TODO.md").exists()


@pytest.mark.asyncio
async def test_todo_write_updates_existing_plan_items_after_leaving_plan_mode(
    tmp_path: Path,
) -> None:
    from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Plan\n- [ ] implement the change\n", encoding="utf-8")
    context = ToolExecutionContext(
        cwd=tmp_path,
        # The model may still pass the old default explicitly here.
        metadata={"permission_mode": "full_auto", "plan_file": str(plan_file)},
    )

    result = await TodoWriteTool().execute(
        TodoWriteToolInput(item="implement the change", checked=True, path="TODO.md"),
        context,
    )

    assert result.metadata["path"] == str(plan_file)
    assert "- [x] implement the change" in plan_file.read_text(encoding="utf-8")
    assert not (tmp_path / "TODO.md").exists()


@pytest.mark.asyncio
async def test_todo_write_explicit_path_still_targets_project_file(tmp_path: Path) -> None:
    from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Plan\n- [ ] plan item\n", encoding="utf-8")
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"permission_mode": "full_auto", "plan_file": str(plan_file)},
    )

    await TodoWriteTool().execute(
        TodoWriteToolInput(item="project task", path="TODO.md"),
        context,
    )

    assert "- [ ] project task" in (tmp_path / "TODO.md").read_text(encoding="utf-8")
    assert "- [ ] project task" not in plan_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_todo_write_marks_done_with_case_and_whitespace_drift(tmp_path: Path) -> None:
    from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Plan\n- [ ] Implement  the change\n", encoding="utf-8")
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"permission_mode": "plan", "plan_file": str(plan_file)},
    )

    result = await TodoWriteTool().execute(
        TodoWriteToolInput(item="implement the change", checked=True),
        context,
    )

    assert result.is_error is False
    content = plan_file.read_text(encoding="utf-8")
    assert "- [x] Implement  the change" in content
    assert "- [ ] Implement  the change" not in content
