"""Tests for the cron scheduler daemon."""

from __future__ import annotations

import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from openharness.services.cron_scheduler import (
    _command_for_job,
    _jobs_due,
    append_history,
    execute_job,
    load_history,
    run_scheduler_loop,
    start_daemon,
)


@pytest.fixture(autouse=True)
def _tmp_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect data and log directories to temp."""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    monkeypatch.setattr("openharness.services.cron_scheduler.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("openharness.services.cron_scheduler.get_logs_dir", lambda: logs_dir)
    # Also redirect the cron registry used by the scheduler
    monkeypatch.setattr(
        "openharness.services.cron.get_cron_registry_path",
        lambda: data_dir / "cron_jobs.json",
    )


def test_shim_turn_job_runs_openharness_in_print_mode() -> None:
    job = {
        "name": "loop-test",
        "cwd": "/tmp/repo",
        "payload": {
            "kind": "shim_turn",
            "message": "check the build",
            "model": "test-model",
            "effort": "high",
            "personality": "shim",
            "permission_mode": "full_auto",
            "tool_profile": "core",
        },
    }

    parts = shlex.split(_command_for_job(job))

    assert parts[:3] == [sys.executable, "-m", "openharness"]
    assert parts[parts.index("--cwd") + 1] == "/tmp/repo"
    assert parts[parts.index("--print") + 1] == "check the build"
    assert parts[parts.index("--model") + 1] == "test-model"
    assert parts[parts.index("--effort") + 1] == "high"
    assert parts[parts.index("--personality") + 1] == "shim"
    assert parts[parts.index("--permission-mode") + 1] == "full_auto"
    assert parts[parts.index("--tool-profile") + 1] == "core"


class TestHistory:
    def test_empty_history(self) -> None:
        assert load_history() == []

    def test_append_and_load(self) -> None:
        append_history({"name": "j1", "status": "success"})
        append_history({"name": "j2", "status": "failed"})
        entries = load_history()
        assert len(entries) == 2
        assert entries[0]["name"] == "j1"

    def test_filter_by_name(self) -> None:
        append_history({"name": "j1", "status": "success"})
        append_history({"name": "j2", "status": "success"})
        entries = load_history(job_name="j1")
        assert len(entries) == 1
        assert entries[0]["name"] == "j1"

    def test_limit(self) -> None:
        for i in range(10):
            append_history({"name": f"j{i}", "status": "success"})
        entries = load_history(limit=3)
        assert len(entries) == 3
        # Should be the last 3
        assert entries[0]["name"] == "j7"


class TestJobsDue:
    def test_due_job(self) -> None:
        now = datetime.now(UTC)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 1

    def test_future_job_not_due(self) -> None:
        now = datetime.now(UTC)
        future = (now + timedelta(hours=1)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True, "next_run": future},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_disabled_job_not_due(self) -> None:
        now = datetime.now(UTC)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": False, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_invalid_schedule_skipped(self) -> None:
        now = datetime.now(UTC)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "not valid", "enabled": True, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_missing_next_run_skipped(self) -> None:
        now = datetime.now(UTC)
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0


class TestExecuteJob:
    @pytest.mark.asyncio
    async def test_successful_job(self) -> None:
        job = {"name": "echo-test", "command": "echo hello", "cwd": "/tmp"}
        entry = await execute_job(job)
        assert entry["status"] == "success"
        assert entry["returncode"] == 0
        assert "hello" in entry["stdout"]

    @pytest.mark.asyncio
    async def test_failing_job(self) -> None:
        job = {"name": "fail-test", "command": "exit 1", "cwd": "/tmp"}
        entry = await execute_job(job)
        assert entry["status"] == "failed"
        assert entry["returncode"] == 1

    @pytest.mark.asyncio
    async def test_timeout_job(self) -> None:
        with patch("openharness.services.cron_scheduler.asyncio.wait_for") as mock_wait:

            mock_wait.side_effect = TimeoutError()

            # Need to mock create_subprocess_exec to return a mock process
            mock_process = AsyncMock()
            mock_process.communicate = Mock(return_value=object())
            mock_process.kill = Mock()
            mock_process.wait = AsyncMock()
            with patch(
                "openharness.utils.shell.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ):
                job = {"name": "slow-test", "command": "sleep 999", "cwd": "/tmp"}
                entry = await execute_job(job)
                assert entry["status"] == "timeout"


class TestSchedulerLoop:
    @pytest.mark.asyncio
    async def test_once_mode_with_no_jobs(self) -> None:
        """Scheduler loop in once-mode should complete without error when no jobs exist."""
        await run_scheduler_loop(once=True)

    @pytest.mark.asyncio
    async def test_once_mode_fires_due_job(self) -> None:
        """Scheduler loop should fire a job that is due."""
        from openharness.services.cron import upsert_cron_job

        upsert_cron_job({"name": "test-once", "schedule": "* * * * *", "command": "echo fired"})

        # Force next_run to the past so it's immediately due
        from openharness.services.cron import load_cron_jobs, save_cron_jobs

        jobs = load_cron_jobs()
        now = datetime.now(UTC)
        jobs[0]["next_run"] = (now - timedelta(minutes=1)).isoformat()
        save_cron_jobs(jobs)

        await run_scheduler_loop(once=True)

        entries = load_history(job_name="test-once")
        assert len(entries) == 1
        assert entries[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_once_mode_handles_signal_registration_failures(self) -> None:
        """Signal handler setup should not block the scheduler loop on unsupported platforms."""
        with patch("openharness.services.cron_scheduler.signal.signal", side_effect=ValueError()):
            await run_scheduler_loop(once=True)


class TestDaemonStartup:
    def test_start_daemon_uses_portable_process_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows startup should use subprocess spawning instead of fork."""
        monkeypatch.setattr("openharness.services.cron_scheduler.get_platform", lambda: "windows")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)

        fake_process = Mock()
        fake_process.pid = 4321
        fake_process.poll.side_effect = [None]

        spawned: dict[str, object] = {}

        def _fake_popen(argv, **kwargs):
            spawned["argv"] = argv
            spawned["kwargs"] = kwargs
            return fake_process

        with patch("openharness.services.cron_scheduler.subprocess.Popen", side_effect=_fake_popen), patch(
            "openharness.services.cron_scheduler.read_pid",
            side_effect=[None, 4321],
        ):
            pid = start_daemon()

        assert pid == 4321
        assert spawned["argv"] == [sys.executable, "-m", "openharness.services.cron_scheduler"]
        assert spawned["kwargs"]["creationflags"] == 0x00000208
        assert spawned["kwargs"]["stdin"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stdout"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stderr"] is subprocess.DEVNULL
        assert spawned["kwargs"]["close_fds"] is True


def test_cron_command_uses_powershell_quoting(monkeypatch):
    monkeypatch.setattr(
        "openharness.services.cron_scheduler.resolve_shell_family",
        lambda: "powershell",
    )
    job = {
        "name": "loop-pwsh",
        "cwd": "C:/repo",
        "payload": {
            "kind": "shim_turn",
            "message": "check the build's state",
        },
    }

    command = _command_for_job(job)

    assert "'C:/repo'" in command
    assert "'check the build''s state'" in command
