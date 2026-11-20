"""Workspace-leftover detection: counts what the session added, not what it touched."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from openharness.engine import session_hygiene


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "readme.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.mark.asyncio
async def test_baseline_is_captured_once(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        nonlocal calls
        calls += 1
        return session_hygiene.WorkspaceSnapshot(worktrees=1, stashes=0)

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    metadata: dict[str, object] = {}

    await session_hygiene.ensure_baseline(metadata, tmp_path)
    await session_hygiene.ensure_baseline(metadata, tmp_path)

    assert calls == 1
    assert metadata[session_hygiene.HYGIENE_KEY]["baseline"] == {"worktrees": 1, "stashes": 0}


@pytest.mark.asyncio
async def test_parallel_tool_calls_share_one_baseline_probe(
    tmp_path: Path, monkeypatch
) -> None:
    """Two tools finishing together must not spawn two git probes at once.

    Concurrent `git worktree list` processes deadlocked Git for Windows in the
    field; the second caller can simply move on because the baseline is
    best-effort and the first probe will record it.
    """
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return session_hygiene.WorkspaceSnapshot(worktrees=1, stashes=0)

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    metadata: dict[str, object] = {}

    probe = asyncio.create_task(session_hygiene.ensure_baseline(metadata, tmp_path))
    await started.wait()

    await session_hygiene.ensure_baseline(metadata, tmp_path)
    assert calls == 1
    assert metadata[session_hygiene.HYGIENE_KEY]["probing"] is True

    release.set()
    await probe

    assert calls == 1
    assert metadata[session_hygiene.HYGIENE_KEY]["baseline"] == {"worktrees": 1, "stashes": 0}
    assert "probing" not in metadata[session_hygiene.HYGIENE_KEY]


@pytest.mark.asyncio
async def test_git_probe_never_inherits_the_protocol_pipe(monkeypatch) -> None:
    """A wrapper/orphan git must not be able to hold the TUI's pipes open."""

    captured: dict[str, object] = {}

    async def fake_spawn(*args, **kwargs):
        captured.update(kwargs)
        raise FileNotFoundError("git")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    assert await session_hygiene._git(".", ["worktree", "list"]) is None
    assert captured["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["stderr"] is asyncio.subprocess.DEVNULL
    assert asyncio.subprocess.PIPE not in captured.values()


@pytest.mark.asyncio
async def test_nothing_new_reports_nothing(tmp_path: Path, monkeypatch) -> None:
    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        return session_hygiene.WorkspaceSnapshot(worktrees=1, stashes=0)

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    metadata: dict[str, object] = {}
    await session_hygiene.ensure_baseline(metadata, tmp_path)

    assert await session_hygiene.collect(metadata, tmp_path) == []


@pytest.mark.asyncio
async def test_a_new_worktree_is_reported_with_its_name(tmp_path: Path, monkeypatch) -> None:
    counts = [1, 2]

    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        return session_hygiene.WorkspaceSnapshot(worktrees=counts.pop(0), stashes=0)

    async def paths(path: object) -> list[str]:
        return ["oh-gate"]

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    monkeypatch.setattr(session_hygiene, "_worktree_paths", paths)
    metadata: dict[str, object] = {}
    await session_hygiene.ensure_baseline(metadata, tmp_path)

    notes = await session_hygiene.collect(metadata, tmp_path)

    assert notes == ["1 git worktree(s) still registered (oh-gate)"]
    assert "Remove them" in session_hygiene.render(notes)


@pytest.mark.asyncio
async def test_a_new_stash_is_reported(tmp_path: Path, monkeypatch) -> None:
    counts = [0, 2]

    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        return session_hygiene.WorkspaceSnapshot(worktrees=1, stashes=counts.pop(0))

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    metadata: dict[str, object] = {}
    await session_hygiene.ensure_baseline(metadata, tmp_path)

    assert await session_hygiene.collect(metadata, tmp_path) == ["2 new git stash entry(ies)"]


@pytest.mark.asyncio
async def test_disabled_check_does_no_work(tmp_path: Path, monkeypatch) -> None:
    async def snapshot(path: object) -> session_hygiene.WorkspaceSnapshot:
        raise AssertionError("must not touch git when disabled")

    monkeypatch.setattr(session_hygiene, "_snapshot", snapshot)
    metadata: dict[str, object] = {}

    await session_hygiene.ensure_baseline(metadata, tmp_path, enabled=False)
    assert await session_hygiene.collect(metadata, tmp_path, enabled=False) == []
    assert metadata == {}


@pytest.mark.asyncio
async def test_outside_a_repository_the_check_gives_up_once(tmp_path: Path) -> None:
    metadata: dict[str, object] = {}

    await session_hygiene.ensure_baseline(metadata, tmp_path)

    state = metadata[session_hygiene.HYGIENE_KEY]
    assert state.get("skipped") is True
    assert await session_hygiene.collect(metadata, tmp_path) == []


@needs_git
def test_real_git_worktree_is_detected(tmp_path: Path) -> None:
    # Sync on purpose: the test drives git itself, and a blocking call inside an
    # async test would stall the loop (and be flagged by ruff).
    root = repo(tmp_path)
    metadata: dict[str, object] = {}
    asyncio.run(session_hygiene.ensure_baseline(metadata, root))

    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tmp_path / "wt"), "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    try:
        notes = asyncio.run(session_hygiene.collect(metadata, root))
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tmp_path / "wt")],
            cwd=root,
            check=False,
            capture_output=True,
        )

    assert notes == ["1 git worktree(s) still registered (wt)"]
