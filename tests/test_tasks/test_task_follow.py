"""Incremental task-log following for /agents follow."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from openharness.services import task_follow
from openharness.services.task_follow import (
    TaskFollower,
    active_task_follows,
    start_task_follow,
    stop_all_task_followers,
    stop_task_follow,
)


@dataclass
class _FakeTask:
    id: str
    output_file: Path
    type: str = "local_agent"
    status: str = "running"
    description: str = "test agent"
    started_at: float = 0.0
    ended_at: float | None = None
    return_code: int | None = None


@pytest.fixture(autouse=True)
async def _clear_followers():
    yield
    await stop_all_task_followers()


async def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for follow output")
        await asyncio.sleep(0.01)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


@pytest.mark.asyncio
async def test_follow_streams_new_lines_and_finishes(tmp_path: Path):
    log_path = tmp_path / "a1.log"
    log_path.write_text("first line\n", encoding="utf-8")
    task = _FakeTask(id="a1", output_file=log_path)
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    follower = TaskFollower(
        "a1", emit, interval=0.01, task_lookup=lambda _: task
    )
    follower.start()
    await _wait_for(lambda: any("first line" in item for item in emitted))

    _append(log_path, "second line\n")
    await _wait_for(lambda: any("second line" in item for item in emitted))

    task.status = "completed"
    task.ended_at = task.started_at + 1.0
    task.return_code = 0
    await _wait_for(lambda: any("done:" in item for item in emitted))

    assert follower.done
    joined = "\n".join(emitted)
    assert "streaming new output" in joined
    assert "[a1] first line" in joined
    assert "[a1] second line" in joined
    assert "completed" in joined


@pytest.mark.asyncio
async def test_follow_streams_partial_lines_without_duplicating_them(tmp_path: Path):
    log_path = tmp_path / "a2.log"
    log_path.write_text("", encoding="utf-8")
    task = _FakeTask(id="a2", output_file=log_path)
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    follower = TaskFollower(
        "a2", emit, interval=0.01, task_lookup=lambda _: task
    )
    follower.start()
    await _wait_for(lambda: any("streaming new output" in item for item in emitted))

    _append(log_path, "partial text")
    await _wait_for(lambda: any("… partial text" in item for item in emitted))

    _append(log_path, " continues here\n")
    await _wait_for(lambda: any("continues here" in item for item in emitted))

    # The completed line is split at the already-streamed boundary, so the
    # transcript does not repeat "partial text".
    completed = [item for item in emitted if "continues here" in item]
    assert all("… partial text" not in item for item in completed)
    assert not any("partial text continues here" in item for item in emitted)

    await follower.stop()


@pytest.mark.asyncio
async def test_follow_reports_a_missing_task(tmp_path: Path):
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    follower = TaskFollower(
        "missing",
        emit,
        interval=0.01,
        task_lookup=lambda _: None,
        tasks_dir=lambda: tmp_path,
    )
    follower.start()
    await _wait_for(lambda: follower.done)

    assert follower.error == "No task found with ID: missing"
    assert "[follow] No task found with ID: missing" in emitted


@pytest.mark.asyncio
async def test_follow_registry_dedupes_and_stops(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "a3.log"
    log_path.write_text("", encoding="utf-8")
    task = _FakeTask(id="a3", output_file=log_path)
    monkeypatch.setattr(task_follow, "_default_task_lookup", lambda _: task)

    async def emit(text: str) -> None:
        return None

    start_task_follow("a3", emit, interval=0.01)
    assert active_task_follows() == ["a3"]
    with pytest.raises(ValueError, match="Already following"):
        start_task_follow("a3", emit, interval=0.01)

    stopped = await stop_task_follow("a3")
    assert stopped == ["a3"]
    assert active_task_follows() == []


@pytest.mark.asyncio
async def test_follow_replays_from_the_start_of_the_log(tmp_path: Path):
    log_path = tmp_path / "a4.log"
    log_path.write_text("old line one\nold line two\n", encoding="utf-8")
    task = _FakeTask(id="a4", output_file=log_path)
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    follower = TaskFollower(
        "a4", emit, interval=0.01, task_lookup=lambda _: task
    )
    follower.start()
    await _wait_for(lambda: any("old line two" in item for item in emitted))
    await follower.stop()

    joined = "\n".join(emitted)
    assert "[a4] old line one" in joined
    assert "[a4] old line two" in joined
