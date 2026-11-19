"""Tests for the typed turn-state view."""

from __future__ import annotations

import pytest

from openharness.engine.turn_state import (
    MAX_TRACKED_ACTIVE_ARTIFACTS,
    MAX_TRACKED_READ_FILES,
    MAX_TRACKED_USER_GOALS,
    TurnState,
    parse_spawned_agent_identity,
    record_tool_carryover,
    remember_active_artifact,
    remember_user_goal,
)


def test_none_metadata_is_a_safe_no_op():
    state = TurnState(None)

    state.remember_goal("do the thing")
    state.remember_artifact("src/a.py")
    state.remember_verified_work("checked")
    state.remember_work_log("did something")
    state.set_permission_mode("plan")

    assert state.raw is None
    assert state.focus() == {}


def test_states_do_not_share_default_buckets():
    """A copy of a module-level template would leak goals between sessions."""
    first = {"tool_metadata": {}}
    second = {"tool_metadata": {}}

    remember_user_goal(first["tool_metadata"], "first goal")
    remember_active_artifact(second["tool_metadata"], "b.py")

    assert first["tool_metadata"]["task_focus_state"]["recent_goals"] == ["first goal"]
    assert first["tool_metadata"]["task_focus_state"]["active_artifacts"] == []
    assert second["tool_metadata"]["task_focus_state"]["recent_goals"] == []
    assert second["tool_metadata"]["task_focus_state"]["active_artifacts"] == ["b.py"]


def test_goal_bucket_is_capped_and_deduplicated():
    state = TurnState({})

    for index in range(MAX_TRACKED_USER_GOALS + 3):
        state.remember_goal(f"goal {index}")
    state.remember_goal("goal 9")

    focus = state.focus()
    goals = focus["recent_goals"]
    assert len(goals) == MAX_TRACKED_USER_GOALS
    assert goals[-1] == "goal 9"
    assert focus["goal"] == "goal 9"


def test_artifact_bucket_is_capped():
    state = TurnState({})

    for index in range(MAX_TRACKED_ACTIVE_ARTIFACTS + 4):
        state.remember_artifact(f"artifact-{index}.py")

    artifacts = state.focus()["active_artifacts"]
    assert len(artifacts) == MAX_TRACKED_ACTIVE_ARTIFACTS
    assert artifacts[-1].endswith("11.py")


def test_malformed_focus_state_is_repaired():
    metadata: dict[str, object] = {"task_focus_state": "not a dict"}

    state = TurnState(metadata)
    state.remember_goal("recover")

    focus = metadata["task_focus_state"]
    assert isinstance(focus, dict)
    assert focus["goal"] == "recover"


def test_read_file_entries_replace_by_path_and_are_capped():
    state = TurnState({})

    for index in range(MAX_TRACKED_READ_FILES + 3):
        state.remember_read_file(
            path=f"src/file_{index}.py",
            offset=0,
            limit=20,
            output="line one\nline two",
        )
    state.remember_read_file(path="src/file_2.py", offset=10, limit=5, output="again")

    entries = state.bucket("read_file_state")
    assert len(entries) == MAX_TRACKED_READ_FILES
    matches = [entry for entry in entries if entry["path"] == "src/file_2.py"]
    assert len(matches) == 1
    assert matches[0]["span"] == "lines 11-15"
    assert entries[-1]["path"] == "src/file_2.py"


def test_carryover_records_work_log_and_verified_work_for_read_file():
    context = type("Ctx", (), {"tool_metadata": {}})()

    record_tool_carryover(
        context,
        tool_name="read_file",
        tool_input={"offset": 0, "limit": 10},
        tool_output="alpha\nbeta",
        tool_result_metadata=None,
        is_error=False,
        resolved_file_path="/repo/src/a.py",
    )

    metadata = context.tool_metadata
    assert metadata["read_file_state"][0]["path"] == "/repo/src/a.py"
    assert metadata["recent_work_log"] == ["Read file /repo/src/a.py"]
    assert "Inspected file /repo/src/a.py" in metadata["recent_verified_work"][0]
    assert "/repo/src/a.py" in metadata["task_focus_state"]["active_artifacts"]


def test_carryover_is_skipped_for_errors():
    context = type("Ctx", (), {"tool_metadata": {}})()

    record_tool_carryover(
        context,
        tool_name="read_file",
        tool_input={},
        tool_output="boom",
        tool_result_metadata=None,
        is_error=True,
        resolved_file_path="/repo/src/a.py",
    )

    assert context.tool_metadata == {}


def test_plan_mode_tools_switch_permission_mode():
    context = type("Ctx", (), {"tool_metadata": {}})()

    record_tool_carryover(
        context,
        tool_name="enter_plan_mode",
        tool_input={},
        tool_output="ok",
        tool_result_metadata=None,
        is_error=False,
        resolved_file_path=None,
    )
    assert context.tool_metadata["permission_mode"] == "plan"

    record_tool_carryover(
        context,
        tool_name="exit_plan_mode",
        tool_input={},
        tool_output="ok",
        tool_result_metadata=None,
        is_error=False,
        resolved_file_path=None,
    )
    assert context.tool_metadata["permission_mode"] == "full_auto"
    assert context.tool_metadata["recent_work_log"] == [
        "Entered plan mode",
        "Exited plan mode",
    ]


def test_parse_spawned_agent_identity_prefers_metadata():
    from_metadata = parse_spawned_agent_identity(
        "Spawned agent from-text (task_id=t9)",
        {"agent_id": "a1", "task_id": "t1"},
    )
    from_text = parse_spawned_agent_identity("Spawned agent explorer (task_id=abc123)")

    assert from_metadata == ("a1", "t1")
    assert from_text == ("explorer", "abc123")
    assert parse_spawned_agent_identity("no spawn here") is None


@pytest.mark.parametrize("tool_name", ["agent", "send_message"])
def test_async_agent_tools_record_activity_and_task(tool_name):
    context = type("Ctx", (), {"tool_metadata": {}})()

    record_tool_carryover(
        context,
        tool_name=tool_name,
        tool_input={"description": "explore", "task_id": "t1"},
        tool_output="Spawned agent explorer (task_id=t1)",
        tool_result_metadata={"agent_id": "a1", "task_id": "t1"},
        is_error=False,
        resolved_file_path=None,
    )

    metadata = context.tool_metadata
    assert metadata["async_agent_state"]
    assert metadata["recent_work_log"] == [f"Async agent action via {tool_name}"]
    if tool_name == "agent":
        assert metadata["async_agent_tasks"][0]["task_id"] == "t1"
