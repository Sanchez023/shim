"""Detect git workspaces a session left behind.

A session can finish its task and still leave the repository in a state nobody
asked for: a temporary worktree, a stash to "keep this around", a detached
branch.  None of that shows up in the diff, so it survives review and the next
session inherits it -- exactly the kind of mess that costs someone an hour
before they trust the tree again.

The check is deliberately narrow.  It compares a baseline captured at the first
tool call against the state at turn end, and only looks at *deliberate* global
acts:

* ``git worktree`` entries -- cheap to create, easy to forget, and they keep
  files alive after the directory is gone.
* ``git stash`` entries -- invisible by design.

Untracked files are **not** counted.  Writing a new test file leaves it
untracked until ``git add``, so counting them would fire on ordinary work and
train the reader to ignore the notice.  Artifacts a session writes outside the
repository cannot be attributed reliably either; that gap is intentional.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

HYGIENE_KEY = "session_hygiene"

GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Countable, attributable leftovers."""

    worktrees: int
    stashes: int


def _state(metadata: MutableMapping[str, object] | None) -> dict[str, object] | None:
    if metadata is None:
        return None
    raw = metadata.get(HYGIENE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[HYGIENE_KEY] = raw
    return raw


async def ensure_baseline(
    metadata: MutableMapping[str, object] | None,
    cwd: str | Path,
    *,
    enabled: bool = True,
) -> None:
    """Record the workspace shape once, on the first tool call of the session.

    Failure to run git is recorded as "skipped" rather than retried: a checkout
    outside a repository, or a machine without git, must not pay for this on
    every call.
    """

    if not enabled:
        return
    state = _state(metadata)
    if state is None or state.get("baseline") is not None or state.get("skipped"):
        return
    # Parallel tool calls reach this point at the same moment.  Spawning
    # several git processes at once is what deadlocked Git for Windows in the
    # field (two `worktree list` probes wedged before doing any I/O), and the
    # baseline only needs to be captured once, so let the first caller probe
    # and the others move on.
    if state.get("probing"):
        return
    state["probing"] = True
    try:
        snapshot = await _snapshot(cwd)
    finally:
        state.pop("probing", None)
    if snapshot is None:
        state["skipped"] = True
        return
    state["baseline"] = {"worktrees": snapshot.worktrees, "stashes": snapshot.stashes}


async def collect(
    metadata: Mapping[str, object] | None,
    cwd: str | Path,
    *,
    enabled: bool = True,
) -> list[str]:
    """Return one human-readable line per leftover this session introduced."""

    if not enabled or metadata is None:
        return []
    raw = metadata.get(HYGIENE_KEY)
    if not isinstance(raw, dict):
        return []
    baseline = raw.get("baseline")
    if not isinstance(baseline, dict):
        return []
    snapshot = await _snapshot(cwd)
    if snapshot is None:
        return []

    notes: list[str] = []
    extra_worktrees = snapshot.worktrees - _as_int(baseline.get("worktrees"))
    if extra_worktrees > 0:
        names = await _worktree_paths(cwd)
        detail = f" ({', '.join(names[:3])})" if names else ""
        notes.append(f"{extra_worktrees} git worktree(s) still registered{detail}")
    extra_stashes = snapshot.stashes - _as_int(baseline.get("stashes"))
    if extra_stashes > 0:
        notes.append(f"{extra_stashes} new git stash entry(ies)")
    return notes


def render(notes: list[str]) -> str:
    """One-line summary for the completion notice."""

    if not notes:
        return ""
    return "Leftovers: " + "; ".join(notes) + ". Remove them, or state in one line why they stay."


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def _snapshot(cwd: str | Path) -> WorkspaceSnapshot | None:
    worktrees = await _git_line_count(cwd, ["worktree", "list", "--porcelain"], prefix="worktree ")
    if worktrees is None:
        return None
    stashes = await _git_line_count(cwd, ["stash", "list"])
    if stashes is None:
        return None
    return WorkspaceSnapshot(worktrees=worktrees, stashes=stashes)


async def _worktree_paths(cwd: str | Path) -> list[str]:
    output = await _git(cwd, ["worktree", "list", "--porcelain"])
    if not output:
        return []
    paths: list[str] = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :]).name)
    # The first entry is the repository itself; it existed before the session.
    return paths[1:]


async def _git_line_count(
    cwd: str | Path,
    args: list[str],
    *,
    prefix: str = "",
) -> int | None:
    output = await _git(cwd, args)
    if output is None:
        return None
    return sum(1 for line in output.splitlines() if line.strip() and line.startswith(prefix))


async def _git(cwd: str | Path, args: list[str]) -> str | None:
    """Run one read-only git probe; ``None`` means "git is unavailable here".

    ``stdout`` goes to a temporary file instead of a pipe.  The ``git.exe`` on
    PATH is a wrapper that starts the real ``git.exe`` as a child, and the
    child inherits the wrapper's handles: if the wrapper is killed after a
    timeout, an orphan can keep a pipe's write end open forever, so
    ``communicate()`` never sees EOF and its cancellation hangs with no timer
    left to rescue the turn.  A file has no EOF contract, so ``wait()`` and
    the timeout always resolve.  ``stdin`` is closed for the same reason —
    these probes must never read (or inherit) the TUI's protocol pipe.
    """

    with tempfile.TemporaryFile() as output:
        try:
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "git",
                    *args,
                    cwd=str(cwd),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output,
                    stderr=asyncio.subprocess.DEVNULL,
                    # A process group lets the timeout kill the whole tree on
                    # POSIX; Windows gets the same effect from taskkill /T.
                    start_new_session=sys.platform != "win32",
                ),
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, TimeoutError):
            return None
        try:
            await asyncio.wait_for(process.wait(), timeout=GIT_TIMEOUT_SECONDS)
        except TimeoutError:
            await _kill_process_tree(process)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=GIT_TIMEOUT_SECONDS
                )
            except TimeoutError:
                return None
            return None
        if process.returncode != 0:
            return None
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill *process* and any wrapper child it left behind.

    Windows has no process groups to signal, and the git wrapper forwards its
    handles to a grandchild, so killing only the direct child can leave that
    grandchild holding the workspace's pipes.  ``taskkill /T`` walks the tree
    before the orphan can outlive the timeout.
    """
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=GIT_TIMEOUT_SECONDS)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ValueError, TimeoutError):
        pass
    try:
        process.kill()
    except OSError:
        pass


__all__ = [
    "HYGIENE_KEY",
    "WorkspaceSnapshot",
    "collect",
    "ensure_baseline",
    "render",
]
