"""Human-readable summaries of background task state.

The task tools and slash commands report on long-running subprocess work. A
bare output tail is not enough to tell what a task is, whether it is still
running, or why it stopped, so the helpers here produce the compact header
that every task-facing surface shares.
"""

from __future__ import annotations

import time
from typing import Any, cast

_TERMINAL_STATUSES = {"completed", "failed", "killed"}


def format_duration(seconds: float | None) -> str:
    """Render a duration in a compact, human-readable form."""
    if seconds is None:
        return ""
    if seconds < 1:
        return f"{max(0, round(seconds * 1000))}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = round(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def task_elapsed_seconds(task: Any, *, now: float | None = None) -> float | None:
    """Return how long a task has been (or was) running, if known."""
    started = getattr(task, "started_at", None) or getattr(task, "created_at", None)
    if not started:
        return None
    ended = getattr(task, "ended_at", None)
    if ended is None:
        ended = now if now is not None else time.time()
    return max(0.0, float(ended) - float(started))


def format_task_summary(
    task: Any,
    *,
    task_id: str | None = None,
    now: float | None = None,
) -> str:
    """Return a multi-line header describing a task's identity and state.

    Fields are read defensively with ``getattr`` so alternate managers (and
    tests) can pass minimal record-like objects.
    """
    resolved_id = task_id or getattr(task, "id", None) or "unknown"
    task_type = getattr(task, "type", None) or "task"
    status = str(getattr(task, "status", None) or "unknown")
    metadata = getattr(task, "metadata", None) or {}
    return_code = getattr(task, "return_code", None)
    elapsed = task_elapsed_seconds(task, now=now)

    header = f"[task {resolved_id}] {task_type} · {status}"
    if status in _TERMINAL_STATUSES:
        if return_code is not None:
            header += f" (exit {return_code})"
        if elapsed is not None:
            header += f" · took {format_duration(elapsed)}"
    elif elapsed is not None:
        header += f" · running for {format_duration(elapsed)}"

    lines = [header]

    description = str(getattr(task, "description", "") or "").strip()
    if description:
        lines.append(f"description: {description}")

    details: list[str] = []
    progress = metadata.get("progress")
    if progress not in (None, ""):
        details.append(f"progress {progress}%")
    note = str(metadata.get("status_note") or "").strip()
    if note:
        details.append(f"note: {note}")
    if details:
        lines.append(" · ".join(details))

    restarts = metadata.get("restart_count")
    if restarts not in (None, "", "0"):
        lines.append(f"restarts: {restarts}")

    return "\n".join(lines)


def read_task_output_with_meta(
    manager: Any,
    task_id: str,
    *,
    max_bytes: int = 12000,
) -> tuple[str, int, bool]:
    """Return ``(output_tail, total_chars, truncated)`` for a task.

    Prefers the manager's metadata-aware reader and falls back to the plain
    reader for alternate/test managers that only implement the older API.
    """
    reader = getattr(manager, "read_task_output_with_meta", None)
    if callable(reader):
        return cast(tuple[str, int, bool], reader(task_id, max_bytes=max_bytes))
    return manager.read_task_output(task_id, max_bytes=max_bytes), 0, False


def truncation_note(*, max_bytes: int, total_chars: int) -> str:
    """Describe a clipped output tail without implying the task failed."""
    return (
        f"[output truncated: showing the last {max_bytes} of "
        f"{total_chars} characters; raise max_bytes to read more]"
    )
