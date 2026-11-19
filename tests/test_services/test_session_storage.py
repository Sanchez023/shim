"""Tests for session persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.services.session_storage import (
    export_session_markdown,
    get_project_session_dir,
    load_session_by_id,
    load_session_snapshot,
    resumable_tool_metadata,
    save_session_snapshot,
)


def test_save_and_load_session_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "repo"
    project.mkdir()

    path = save_session_snapshot(
        cwd=project,
        model="claude-test",
        system_prompt="system",
        messages=[ConversationMessage(role="user", content=[TextBlock(text="hello")])],
        usage=UsageSnapshot(input_tokens=1, output_tokens=2),
        tool_metadata={
            "task_focus_state": {"goal": "Fix compact carry-over"},
            "recent_verified_work": ["Focused session storage test passed"],
        },
    )

    assert path.exists()
    snapshot = load_session_snapshot(project)
    assert snapshot is not None
    assert snapshot["model"] == "claude-test"
    assert snapshot["usage"]["output_tokens"] == 2
    assert snapshot["tool_metadata"]["task_focus_state"]["goal"] == "Fix compact carry-over"
    assert snapshot["tool_metadata"]["recent_verified_work"] == ["Focused session storage test passed"]


def test_export_session_markdown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "repo"
    project.mkdir()

    path = export_session_markdown(
        cwd=project,
        messages=[
            ConversationMessage(role="user", content=[TextBlock(text="hello")]),
            ConversationMessage(role="assistant", content=[TextBlock(text="world")]),
        ],
    )

    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "OpenHarness Session Transcript" in content
    assert "hello" in content
    assert "world" in content


def test_load_session_snapshot_sanitizes_legacy_empty_assistant_messages(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "repo"
    project.mkdir()

    target_dir = get_project_session_dir(project)
    payload = {
        "session_id": "legacy123",
        "cwd": str(project),
        "model": "claude-test",
        "system_prompt": "system",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": []},
            {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "tool_metadata": {},
        "created_at": 1.0,
        "summary": "hello",
        "message_count": 4,
    }
    (target_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")

    snapshot = load_session_snapshot(project)
    assert snapshot is not None
    assert snapshot["message_count"] == 2
    assert [message["role"] for message in snapshot["messages"]] == ["user", "assistant"]
    assert snapshot["messages"][1]["content"][0]["text"] == "world"


def test_resumable_tool_metadata_keeps_work_state_only():
    payload = {
        "permission_mode": "plan",
        "plan_file": "/tmp/sessions/plan-abc.md",
        "read_file_state": [{"path": "src/app.py"}],
        "recent_work_log": ["edited src/app.py"],
        "task_focus_state": {"goal": "ship it"},
        "unknown_key": "ignored",
    }

    restored = resumable_tool_metadata(payload)

    assert restored == {
        "plan_file": "/tmp/sessions/plan-abc.md",
        "read_file_state": [{"path": "src/app.py"}],
        "recent_work_log": ["edited src/app.py"],
        "task_focus_state": {"goal": "ship it"},
    }
    assert resumable_tool_metadata(None) == {}


@pytest.mark.asyncio
async def test_event_log_wins_over_snapshot_on_resume(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "repo"
    project.mkdir()

    save_session_snapshot(
        cwd=project,
        model="claude-test",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("snapshot message")],
        usage=UsageSnapshot(input_tokens=1, output_tokens=1),
        session_id="sess-log",
    )

    from openharness.domain.events import EventSequencer
    from openharness.services.event_log import (
        JSONLEventLogStore,
        session_event_log_path,
    )

    log_path = session_event_log_path(get_project_session_dir(project), "sess-log")
    store = JSONLEventLogStore(log_path)
    sequencer = EventSequencer(session_id="sess-log")
    await store.append(
        sequencer.next(
            "user.message",
            {
                "message": ConversationMessage.from_user_text(
                    "message from event log"
                ).model_dump(mode="json")
            },
        )
    )

    latest = load_session_snapshot(project)
    assert latest is not None
    assert latest["messages_source"] == "event_log"
    assert latest["message_count"] == 1
    assert latest["messages"][0]["content"][0]["text"] == "message from event log"

    by_id = load_session_by_id(project, "sess-log")
    assert by_id is not None
    assert by_id["messages_source"] == "event_log"
    assert by_id["messages"][0]["content"][0]["text"] == "message from event log"
