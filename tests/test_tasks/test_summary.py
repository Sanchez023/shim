"""Tests for task summary formatting."""

from __future__ import annotations

from pathlib import Path

from openharness.tasks.summary import (
    format_duration,
    format_task_summary,
    task_elapsed_seconds,
)
from openharness.tasks.types import TaskRecord


def _record(**overrides: object) -> TaskRecord:
    defaults: dict[str, object] = {
        "id": "a9f391dda",
        "type": "local_agent",
        "status": "running",
        "description": "Audit runtime behavior surface vs benchmark coverage",
        "cwd": ".",
        "output_file": Path("a9f391dda.log"),
        "created_at": 100.0,
        "started_at": 100.0,
        "metadata": {},
    }
    defaults.update(overrides)
    return TaskRecord(**defaults)  # type: ignore[arg-type]


def test_format_task_summary_for_running_task():
    task = _record(metadata={"progress": "40", "status_note": "reading benchmark spec"})

    text = format_task_summary(task, now=160.0)

    assert "[task a9f391dda] local_agent · running · running for 1m00s" in text
    assert "description: Audit runtime behavior surface vs benchmark coverage" in text
    assert "progress 40%" in text
    assert "note: reading benchmark spec" in text


def test_format_task_summary_for_finished_task():
    task = _record(
        status="completed",
        ended_at=145.0,
        return_code=0,
        metadata={"restart_count": "1"},
    )

    text = format_task_summary(task, now=999.0)

    assert "[task a9f391dda] local_agent · completed (exit 0) · took 45.0s" in text
    assert "restarts: 1" in text


def test_format_task_summary_tolerates_partial_records():
    class _Minimal:
        status = "running"

    text = format_task_summary(_Minimal(), task_id="task-1", now=10.0)

    assert text.startswith("[task task-1] task · running")


def test_task_elapsed_seconds_uses_start_or_creation_time():
    assert task_elapsed_seconds(_record(started_at=None, created_at=10.0), now=15.0) == 5.0
    assert task_elapsed_seconds(_record(started_at=None, created_at=0.0), now=15.0) is None


def test_format_duration():
    assert format_duration(0.25) == "250ms"
    assert format_duration(5.5) == "5.5s"
    assert format_duration(65) == "1m05s"
    assert format_duration(3725) == "1h02m"
