"""Turn-scoped follow-up state carried between tool calls.

The engine remembers what the model just touched - files read, skills loaded,
artifacts produced, goals stated - so the next prompt can say "you were working
on X".  That bookkeeping is independent of the query loop and of the tool
dispatcher, so it lives here instead of inside ``query.py``.

The backing store is still a plain ``dict`` on ``QueryContext.tool_metadata``:
it is serialized into session snapshots and read by several services, so
changing the container would change the persisted format.  :class:`TurnState`
is the only code that knows the key names and value shapes, which means a typo
or a shape drift fails here instead of silently producing an empty list
somewhere downstream.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, Protocol


class ToolMetadataCarrier(Protocol):
    """Anything that carries the mutable per-session tool metadata dict."""

    tool_metadata: dict[str, object] | None


# Bucket caps: enough history to be useful in a prompt, small enough that it
# cannot crowd out the actual conversation.
MAX_TRACKED_READ_FILES = 6
MAX_TRACKED_SKILLS = 8
MAX_TRACKED_ASYNC_AGENT_EVENTS = 8
MAX_TRACKED_ASYNC_AGENT_TASKS = 12
MAX_TRACKED_WORK_LOG = 10
MAX_TRACKED_USER_GOALS = 5
MAX_TRACKED_ACTIVE_ARTIFACTS = 8
MAX_TRACKED_VERIFIED_WORK = 10

# Keys of the persisted metadata dict.  Named so misspellings are greppable.
FOCUS_KEY = "task_focus_state"
READ_FILES_KEY = "read_file_state"
SKILLS_KEY = "invoked_skills"
ASYNC_ACTIVITY_KEY = "async_agent_state"
ASYNC_TASKS_KEY = "async_agent_tasks"
WORK_LOG_KEY = "recent_work_log"
VERIFIED_WORK_KEY = "recent_verified_work"
PERMISSION_MODE_KEY = "permission_mode"

_FOCUS_KEYS: tuple[str, ...] = (
    "goal",
    "recent_goals",
    "active_artifacts",
    "verified_state",
    "next_step",
)


def _fresh_focus() -> dict[str, object]:
    """Build a new focus dict.

    Never copy a module-level template: the list values would be shared between
    every state object, so one session's goals would leak into another.
    """
    return {
        "goal": "",
        "recent_goals": [],
        "active_artifacts": [],
        "verified_state": [],
        "next_step": "",
    }


def _summarize_focus_text(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    return normalized[:240]


class TurnState:
    """Typed view over the per-session follow-up metadata dict.

    Every mutator is a no-op when the underlying dict is ``None``, which is how
    callers express "this run carries no follow-up state".
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: dict[str, object] | None) -> None:
        self._raw = raw

    @property
    def raw(self) -> dict[str, object] | None:
        return self._raw

    # -- generic bucket handling -------------------------------------------

    def bucket(self, key: str) -> list[Any]:
        """Return the mutable list stored at ``key``, creating it if needed.

        A key holding a non-list (older snapshot, hand-edited config) is
        replaced rather than raising: the state is advisory.
        """
        if self._raw is None:
            return []
        value = self._raw.setdefault(key, [])
        if isinstance(value, list):
            return value
        replacement: list[Any] = []
        self._raw[key] = replacement
        return replacement

    def _truncate(self, bucket: list[Any], limit: int) -> None:
        if len(bucket) > limit:
            del bucket[:-limit]

    def push(self, key: str, value: Any, *, limit: int) -> None:
        bucket = self.bucket(key)
        bucket.append(value)
        self._truncate(bucket, limit)

    def push_unique(self, key: str, value: Any, *, limit: int) -> None:
        bucket = self.bucket(key)
        if value in bucket:
            bucket.remove(value)
        bucket.append(value)
        self._truncate(bucket, limit)

    def upsert(
        self,
        key: str,
        value: Any,
        *,
        matches: Callable[[Any], bool],
        limit: int,
    ) -> None:
        bucket = self.bucket(key)
        bucket[:] = [existing for existing in bucket if not matches(existing)]
        bucket.append(value)
        self._truncate(bucket, limit)

    # -- focus state --------------------------------------------------------

    def focus(self) -> dict[str, object]:
        """Return the mutable focus sub-dict, repairing a malformed one."""
        if self._raw is None:
            return {}
        value = self._raw.setdefault(FOCUS_KEY, _fresh_focus())
        if not isinstance(value, dict):
            replacement = _fresh_focus()
            self._raw[FOCUS_KEY] = replacement
            return replacement
        value.setdefault("goal", "")
        value.setdefault("recent_goals", [])
        value.setdefault("active_artifacts", [])
        value.setdefault("verified_state", [])
        value.setdefault("next_step", "")
        return value

    def _focus_list(self, key: str) -> list[Any]:
        focus = self.focus()
        value = focus.setdefault(key, [])
        if isinstance(value, list):
            return value
        replacement: list[Any] = []
        focus[key] = replacement
        return replacement

    # -- recorders ----------------------------------------------------------

    def remember_goal(self, prompt: str) -> None:
        if self._raw is None:
            return
        focus = self.focus()
        summary = _summarize_focus_text(prompt)
        if not summary:
            return
        recent_goals = self._focus_list("recent_goals")
        if summary in recent_goals:
            recent_goals.remove(summary)
        recent_goals.append(summary)
        self._truncate(recent_goals, MAX_TRACKED_USER_GOALS)
        focus["goal"] = summary

    def remember_artifact(self, artifact: str) -> None:
        normalized = artifact.strip()
        if not normalized:
            return
        if self._raw is None:
            return
        artifacts = self._focus_list("active_artifacts")
        value = normalized[:240]
        if value in artifacts:
            artifacts.remove(value)
        artifacts.append(value)
        self._truncate(artifacts, MAX_TRACKED_ACTIVE_ARTIFACTS)

    def remember_verified_work(self, entry: str) -> None:
        normalized = entry.strip()
        if not normalized:
            return
        value = normalized[:320]
        self.push_unique(VERIFIED_WORK_KEY, value, limit=MAX_TRACKED_VERIFIED_WORK)
        if self._raw is None:
            return
        verified_state = self._focus_list("verified_state")
        if value in verified_state:
            verified_state.remove(value)
        verified_state.append(value)
        self._truncate(verified_state, MAX_TRACKED_VERIFIED_WORK)

    def remember_read_file(
        self,
        *,
        path: str,
        offset: int,
        limit: int,
        output: str,
    ) -> None:
        preview_lines = [line.strip() for line in output.splitlines()[:6] if line.strip()]
        entry = {
            "path": path,
            "span": f"lines {offset + 1}-{offset + limit}",
            "preview": " | ".join(preview_lines)[:320],
            "timestamp": time.time(),
        }
        self.upsert(
            READ_FILES_KEY,
            entry,
            matches=lambda existing: (
                isinstance(existing, dict) and str(existing.get("path") or "") == path
            ),
            limit=MAX_TRACKED_READ_FILES,
        )

    def remember_skill(self, skill_name: str) -> None:
        normalized = skill_name.strip()
        if not normalized:
            return
        self.push_unique(SKILLS_KEY, normalized, limit=MAX_TRACKED_SKILLS)

    def remember_async_activity(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        output: str,
    ) -> None:
        if tool_name == "agent":
            description = str(
                tool_input.get("description") or tool_input.get("prompt") or ""
            ).strip()
            summary = f"Spawned async agent. {description}".strip()
            if output.strip():
                summary = f"{summary} [{output.strip()[:180]}]".strip()
        elif tool_name == "send_message":
            target = str(tool_input.get("task_id") or "").strip()
            summary = f"Sent follow-up message to async agent {target}".strip()
        else:
            summary = output.strip()[:220] or f"Async agent activity via {tool_name}"
        self.push(ASYNC_ACTIVITY_KEY, summary, limit=MAX_TRACKED_ASYNC_AGENT_EVENTS)

    def remember_async_task(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        output: str,
        result_metadata: dict[str, object] | None,
    ) -> None:
        if tool_name != "agent":
            return
        identity = parse_spawned_agent_identity(output, result_metadata)
        if identity is None:
            return
        agent_id, task_id = identity
        description = str(
            tool_input.get("description") or tool_input.get("prompt") or ""
        ).strip()
        entry = {
            "agent_id": agent_id,
            "task_id": task_id,
            "description": description[:240],
            "status": "spawned",
            "notification_sent": False,
            "spawned_at": time.time(),
        }
        self.upsert(
            ASYNC_TASKS_KEY,
            entry,
            matches=lambda existing: (
                isinstance(existing, dict) and str(existing.get("task_id") or "") == task_id
            ),
            limit=MAX_TRACKED_ASYNC_AGENT_TASKS,
        )

    def remember_work_log(self, entry: str) -> None:
        bucket = self.bucket(WORK_LOG_KEY)
        normalized = entry.strip()
        if not normalized:
            return
        bucket.append(normalized[:320])
        self._truncate(bucket, MAX_TRACKED_WORK_LOG)

    def set_permission_mode(self, mode: str) -> None:
        if self._raw is None:
            return
        self._raw[PERMISSION_MODE_KEY] = mode

    # -- dispatch -----------------------------------------------------------

    def record_carryover(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        tool_output: str,
        tool_result_metadata: dict[str, object] | None,
        is_error: bool,
        resolved_file_path: str | None,
    ) -> None:
        if is_error:
            return
        if resolved_file_path is not None:
            self.remember_artifact(resolved_file_path)
        if tool_name == "read_file" and resolved_file_path is not None:
            raw_offset = tool_input.get("offset")
            raw_limit = tool_input.get("limit")
            offset = int(raw_offset) if isinstance(raw_offset, (int, float, str)) else 0
            limit = int(raw_limit) if isinstance(raw_limit, (int, float, str)) else 200
            self.remember_read_file(
                path=resolved_file_path,
                offset=offset,
                limit=limit,
                output=tool_output,
            )
            self.remember_verified_work(
                f"Inspected file {resolved_file_path} (lines {offset + 1}-{offset + limit})"
            )
        elif tool_name == "skill":
            skill_name = str(tool_input.get("name") or "").strip()
            self.remember_skill(skill_name)
            if skill_name:
                self.remember_artifact(f"skill:{skill_name}")
                self.remember_verified_work(f"Loaded skill {skill_name}")
        elif tool_name in {"agent", "send_message"}:
            self.remember_async_activity(
                tool_name=tool_name,
                tool_input=tool_input,
                output=tool_output,
            )
            self.remember_async_task(
                tool_name=tool_name,
                tool_input=tool_input,
                output=tool_output,
                result_metadata=tool_result_metadata,
            )
            description = str(
                tool_input.get("description") or tool_input.get("prompt") or tool_name
            ).strip()
            self.remember_verified_work(
                f"Confirmed async-agent activity via {tool_name}: {description[:180]}"
            )
        elif tool_name == "enter_plan_mode":
            self.set_permission_mode("plan")
        elif tool_name == "exit_plan_mode":
            self.set_permission_mode("full_auto")
        elif tool_name == "web_fetch":
            url = str(tool_input.get("url") or "").strip()
            if url:
                self.remember_artifact(url)
                self.remember_verified_work(f"Fetched remote content from {url}")
        elif tool_name == "web_search":
            query = str(tool_input.get("query") or "").strip()
            if query:
                self.remember_verified_work(f"Ran web search for {query[:180]}")
        elif tool_name == "glob":
            pattern = str(tool_input.get("pattern") or "").strip()
            if pattern:
                self.remember_verified_work(f"Expanded glob pattern {pattern[:180]}")
        elif tool_name == "grep":
            pattern = str(tool_input.get("pattern") or "").strip()
            if pattern:
                self.remember_verified_work(
                    f"Checked repository matches for grep pattern {pattern[:180]}"
                )
        elif tool_name == "bash":
            command = str(tool_input.get("command") or "").strip()
            summary = (
                tool_output.splitlines()[0].strip() if tool_output.strip() else "no output"
            )
            self.remember_verified_work(
                f"Ran bash command {command[:160]} [{summary[:120]}]"
            )

        # A second, independent pass records the short work log.  ``read_file``
        # intentionally starts a fresh chain here rather than continuing the
        # one above: a call can both verify work and log it.
        if tool_name == "read_file" and resolved_file_path is not None:
            self.remember_work_log(f"Read file {resolved_file_path}")
        elif tool_name == "bash":
            command = str(tool_input.get("command") or "").strip()
            summary = (
                tool_output.splitlines()[0].strip() if tool_output.strip() else "no output"
            )
            self.remember_work_log(f"Ran bash: {command[:160]} [{summary[:120]}]")
        elif tool_name == "grep":
            pattern = str(tool_input.get("pattern") or "").strip()
            self.remember_work_log(f"Searched with grep pattern={pattern[:160]}")
        elif tool_name == "skill":
            self.remember_work_log(
                f"Loaded skill {str(tool_input.get('name') or '').strip()}"
            )
        elif tool_name in {"agent", "send_message"}:
            self.remember_work_log(f"Async agent action via {tool_name}")
        elif tool_name == "enter_plan_mode":
            self.remember_work_log("Entered plan mode")
        elif tool_name == "exit_plan_mode":
            self.remember_work_log("Exited plan mode")


def parse_spawned_agent_identity(
    output: str,
    metadata: dict[str, object] | None = None,
) -> tuple[str, str] | None:
    """Return ``(agent_id, task_id)`` for a spawned agent, if one is described.

    Prefers structured result metadata and falls back to the human-readable
    spawn line, because older tool versions only emitted the latter.
    """
    if isinstance(metadata, dict):
        agent_id = str(metadata.get("agent_id") or "").strip()
        task_id = str(metadata.get("task_id") or "").strip()
        if agent_id and task_id:
            return agent_id, task_id
    match = re.search(r"Spawned agent (.+?) \(task_id=(\S+?)(?:[,)]|$)", output.strip())
    if match is None:
        return None
    return match.group(1).strip(), match.group(2).strip()


# ---------------------------------------------------------------------------
# Compatibility wrappers
#
# These keep the dict-level call signatures that the dispatcher and other
# services use.  New code should build a ``TurnState`` instead.
# ---------------------------------------------------------------------------


def remember_user_goal(tool_metadata: dict[str, object] | None, prompt: str) -> None:
    TurnState(tool_metadata).remember_goal(prompt)


def remember_active_artifact(
    tool_metadata: dict[str, object] | None,
    artifact: str,
) -> None:
    TurnState(tool_metadata).remember_artifact(artifact)


def record_tool_carryover(
    context: ToolMetadataCarrier,
    *,
    tool_name: str,
    tool_input: dict[str, object],
    tool_output: str,
    tool_result_metadata: dict[str, object] | None,
    is_error: bool,
    resolved_file_path: str | None,
) -> None:
    TurnState(context.tool_metadata).record_carryover(
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
        tool_result_metadata=tool_result_metadata,
        is_error=is_error,
        resolved_file_path=resolved_file_path,
    )
