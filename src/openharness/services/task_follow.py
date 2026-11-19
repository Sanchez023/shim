"""Stream a background task's log into the active transcript.

Task agents run as separate processes and write plain text to
``<data>/tasks/<task_id>.log``.  The parent TUI only sees the ``agent`` tool
call, so there is no built-in way to watch the child work.  This module polls
that log and forwards new content through the runtime's system printer, which
is already rendered as transcript items in the React TUI and printed to stdout
in headless mode.

Following is opt-in (``/agents follow <task_id>``) and stops on task
completion, an explicit ``/agents unfollow``, or runtime shutdown.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from openharness.config.paths import get_tasks_dir
from openharness.tasks import get_task_manager
from openharness.tasks.summary import format_task_summary

log = logging.getLogger(__name__)

DEFAULT_FOLLOW_INTERVAL_SECONDS = 0.75
DEFAULT_MAX_BYTES_PER_POLL = 32_768

TERMINAL_STATUSES = {"completed", "failed", "killed"}
AGENT_TASK_TYPES = {"local_agent", "remote_agent", "in_process_teammate"}

TaskLookup = Callable[[str], Any | None]
TasksDir = Callable[[], Path]
EmitLine = Callable[[str], Awaitable[None]]


class TaskFollower:
    """Poll one task log and emit new lines through ``emit``."""

    def __init__(
        self,
        task_id: str,
        emit: EmitLine,
        *,
        interval: float = DEFAULT_FOLLOW_INTERVAL_SECONDS,
        max_bytes_per_poll: int = DEFAULT_MAX_BYTES_PER_POLL,
        task_lookup: TaskLookup | None = None,
        tasks_dir: TasksDir | None = None,
    ) -> None:
        self.task_id = task_id
        self._emit = emit
        self._interval = max(0.05, float(interval))
        self._max_bytes = max(1_024, int(max_bytes_per_poll))
        self._lookup = task_lookup or _default_task_lookup
        self._tasks_dir = tasks_dir or get_tasks_dir
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._offset = 0
        self._pending = ""
        self._partial_sent = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.error: str | None = None

    def start(self) -> None:
        """Start polling in the background."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"task-follow-{self.task_id}"
            )

    @property
    def done(self) -> bool:
        """Return True when the follower is no longer running."""
        return self._task is None or self._task.done()

    async def stop(self) -> None:
        """Stop polling and wait for the task to finish."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _log_path(self, task: Any) -> Path:
        output_file = getattr(task, "output_file", None)
        if output_file:
            return Path(output_file)
        return self._tasks_dir() / f"{self.task_id}.log"

    def _read_new(self, path: Path) -> tuple[list[str], bool]:
        """Return ``(display_chunks, at_eof)`` for newly appended bytes."""
        try:
            size = path.stat().st_size
        except OSError:
            return [], True
        if size < self._offset:
            # The log was truncated or replaced; start over.
            self._offset = 0
            self._pending = ""
            self._partial_sent = 0
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(self._max_bytes)
        except OSError:
            return [], True
        if not chunk:
            return [], True
        self._offset += len(chunk)
        text = self._decoder.decode(chunk)
        self._pending += text
        parts = self._pending.split("\n")
        self._pending = parts.pop()

        out: list[str] = []
        if parts:
            # The first part may continue a partial line that was already
            # streamed, so only send the unseen remainder.
            first = parts[0].rstrip("\r")
            remainder = first[self._partial_sent :] if self._partial_sent else first
            if remainder.strip():
                out.append(remainder)
            for part in parts[1:]:
                line = part.rstrip("\r")
                if line.strip():
                    out.append(line)
            self._partial_sent = 0

        if self._pending and len(self._pending) > self._partial_sent:
            suffix = self._pending[self._partial_sent :]
            if suffix.strip():
                out.append(f"… {suffix}")
            self._partial_sent = len(self._pending)
        return out, len(chunk) < self._max_bytes

    async def _run(self) -> None:
        task = self._lookup(self.task_id)
        if task is None:
            self.error = f"No task found with ID: {self.task_id}"
            await self._emit(f"[follow] {self.error}")
            return

        path = self._log_path(task)
        await self._emit(
            f"[follow] {format_task_summary(task, task_id=self.task_id)}\n"
            f"[follow] log: {path}\n"
            f"[follow] streaming new output; stop with "
            f"/agents unfollow {self.task_id}"
        )

        while not self._stop.is_set():
            task = self._lookup(self.task_id) or task
            status = str(getattr(task, "status", "") or "")
            terminal = status in TERMINAL_STATUSES
            chunks, at_eof = self._read_new(path)
            if terminal and self._pending:
                # Flush a final line that was written without a newline.
                tail = self._pending[self._partial_sent :]
                if tail.strip():
                    chunks.append(tail)
                self._pending = ""
                self._partial_sent = 0
            if chunks:
                body = "\n".join(f"[{self.task_id}] {chunk}" for chunk in chunks)
                try:
                    await self._emit(body)
                except Exception:
                    log.info("Task follow stopped: transcript unavailable", exc_info=True)
                    return
            if terminal and at_eof:
                await self._emit(
                    f"[follow] done: {format_task_summary(task, task_id=self.task_id)}"
                )
                return
            if not at_eof:
                # Caught up on a large chunk; keep reading without sleeping.
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue


def _default_task_lookup(task_id: str) -> Any | None:
    return get_task_manager().get_task(task_id)


_FOLLOWERS: dict[str, TaskFollower] = {}


def start_task_follow(
    task_id: str,
    emit: EmitLine,
    *,
    interval: float = DEFAULT_FOLLOW_INTERVAL_SECONDS,
) -> TaskFollower:
    """Start following ``task_id``; raise when it is already followed."""
    normalized = task_id.strip()
    existing = _FOLLOWERS.get(normalized)
    if existing is not None and not existing.done:
        raise ValueError(f"Already following task {normalized}")
    follower = TaskFollower(normalized, emit, interval=interval)
    _FOLLOWERS[normalized] = follower
    follower.start()
    return follower


def active_task_follows() -> list[str]:
    """Return the ids that are currently followed."""
    finished = [task_id for task_id, item in _FOLLOWERS.items() if item.done]
    for task_id in finished:
        _FOLLOWERS.pop(task_id, None)
    return sorted(_FOLLOWERS)


async def stop_task_follow(task_id: str | None = None) -> list[str]:
    """Stop one follow, or all of them when ``task_id`` is None."""
    targets = [task_id.strip()] if task_id and task_id.strip() else list(_FOLLOWERS)
    stopped: list[str] = []
    for target in targets:
        follower = _FOLLOWERS.pop(target, None)
        if follower is None:
            continue
        await follower.stop()
        stopped.append(target)
    return stopped


async def stop_all_task_followers() -> None:
    """Stop every follower (used when the runtime closes)."""
    await stop_task_follow(None)
