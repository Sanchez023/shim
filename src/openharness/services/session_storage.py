"""Session persistence helpers."""

from __future__ import annotations

import json
import time
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.config.paths import get_sessions_dir
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    sanitize_conversation_messages,
)
from openharness.domain.usage import UsageSnapshot
from openharness.utils.fs import atomic_write_text

#: Session summaries are shown as one-line picker labels, so they must never
#: contain the harness-injected per-turn context block or raw newlines.
SUMMARY_MAX_CHARS = 80
_HARNESS_CONTEXT_PREFIXES = ("<turn_context>",)


def _one_line(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Collapse whitespace (including newlines) and clamp to a label size."""
    return " ".join(text.split())[:limit].strip()


def _is_harness_noise(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _HARNESS_CONTEXT_PREFIXES)


def summary_from_messages(messages: list[ConversationMessage]) -> str:
    """Summarize a conversation from its first user-visible user message.

    Harness-injected ``session_private`` blocks (the per-turn context message
    is one) are never shown to the user, so they must not become the label.
    """
    for message in messages:
        if message.role != "user":
            continue
        text = " ".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock)
            and block.visibility == "user_visible"
            and block.text.strip()
        )
        summary = _one_line(text)
        if summary:
            return summary
    return ""


def _summary_from_message_dicts(messages: Any) -> str:
    """Same as :func:`summary_from_messages` for raw snapshot dictionaries."""
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("visibility", "user_visible") == "user_visible"
            and str(block.get("text") or "").strip()
        ]
        summary = _one_line(" ".join(texts))
        if summary:
            return summary
    return ""


def _snapshot_summary(data: dict[str, Any]) -> str:
    """Prefer a freshly derived summary over a stale/harness-generated one."""
    summary = _summary_from_message_dicts(data.get("messages"))
    if summary:
        return summary
    stored = str(data.get("summary") or "")
    if _is_harness_noise(stored):
        return ""
    return _one_line(stored)

_PERSISTED_TOOL_METADATA_KEYS = (
    "permission_mode",
    "plan_file",
    "read_file_state",
    "invoked_skills",
    "async_agent_state",
    "async_agent_tasks",
    "recent_work_log",
    "recent_verified_work",
    "task_focus_state",
    "compact_checkpoints",
    "compact_last",
    "verification_ledger",
    # Last provider-reported prompt size. Guarded by its ``message_count`` so a
    # resumed conversation only uses it while the message list still lines up.
    "prompt_measurement",
)


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)


def _persistable_tool_metadata(tool_metadata: dict[str, object] | None) -> dict[str, Any]:
    if not isinstance(tool_metadata, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in _PERSISTED_TOOL_METADATA_KEYS:
        if key in tool_metadata:
            payload[key] = _sanitize_metadata(tool_metadata[key])
    return payload


def resumable_tool_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return the persisted work state a resume may carry into a live session.

    Only the keys that describe what the model already knows (read files, work
    log, focus state) are returned. ``permission_mode`` is deliberately
    excluded: the live checker owns the current mode, so replaying an old
    snapshot must not make the prompt describe a mode the session is not in.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload[key]
        for key in _PERSISTED_TOOL_METADATA_KEYS
        if key in payload and key != "permission_mode"
    }


def get_project_session_dir(cwd: str | Path) -> Path:
    """Return the session directory for a project."""
    path = Path(cwd).resolve()
    digest = sha1(str(path).encode("utf-8")).hexdigest()[:12]
    session_dir = get_sessions_dir() / f"{path.name}-{digest}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def save_session_snapshot(
    *,
    cwd: str | Path,
    model: str,
    system_prompt: str,
    messages: list[ConversationMessage],
    usage: UsageSnapshot,
    session_id: str | None = None,
    tool_metadata: dict[str, object] | None = None,
) -> Path:
    """Persist a session snapshot. Saves both by ID and as latest."""
    session_dir = get_project_session_dir(cwd)
    sid = session_id or uuid4().hex[:12]
    now = time.time()
    messages = sanitize_conversation_messages(messages)
    summary = summary_from_messages(messages)

    payload = {
        "session_id": sid,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "system_prompt": system_prompt,
        "messages": [message.model_dump(mode="json") for message in messages],
        "usage": usage.model_dump(),
        "tool_metadata": _persistable_tool_metadata(tool_metadata),
        "created_at": now,
        "summary": summary,
        "message_count": len(messages),
    }
    data = json.dumps(payload, indent=2) + "\n"

    # Save by session ID first: ``latest.json`` is a pointer, so writing it
    # before the session file can leave a dangling pointer if the process dies
    # between the two writes (ADR-0012 D7).
    session_path = session_dir / f"session-{sid}.json"
    atomic_write_text(session_path, data)

    latest_path = session_dir / "latest.json"
    atomic_write_text(latest_path, data)

    return latest_path


def _sanitize_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize persisted messages for forward compatibility."""
    raw_messages = payload.get("messages", [])
    if isinstance(raw_messages, list):
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(item) for item in raw_messages]
        )
        payload = dict(payload)
        payload["messages"] = [message.model_dump(mode="json") for message in messages]
        payload["message_count"] = len(messages)
        stored = str(payload.get("summary") or "")
        if not stored or _is_harness_noise(stored):
            summary = summary_from_messages(messages)
            if summary:
                payload["summary"] = summary
    return payload


def _prefer_event_log_messages(
    cwd: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Overlay the event-log projection when a matching log exists.

    ``events.jsonl`` is the message SoT (ADR-0010 D5); the snapshot remains a
    legacy fallback for sessions that predate the event log or whose log is
    missing/empty.
    """
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return payload

    from openharness.services.event_log import JSONLEventLogStore, session_event_log_path
    from openharness.services.projections.session_log import SessionLogState

    path = session_event_log_path(get_project_session_dir(cwd), session_id)
    if not path.exists():
        return payload

    events = JSONLEventLogStore(path).read_all_sync()
    if not events:
        return payload
    state = SessionLogState()
    for event in events:
        state.apply(event)
    messages = state.derive_messages()
    if not messages:
        return payload

    updated = dict(payload)
    updated["messages"] = [message.model_dump(mode="json") for message in messages]
    updated["message_count"] = len(messages)
    updated["messages_source"] = "event_log"
    if not updated.get("summary") or _is_harness_noise(str(updated.get("summary") or "")):
        summary = summary_from_messages(messages)
        if summary:
            updated["summary"] = summary
    return updated


def load_session_snapshot(cwd: str | Path) -> dict[str, Any] | None:
    """Load the most recent session snapshot for the project.

    Legacy fallback: if the session has an event log, its derived messages win.
    """
    path = get_project_session_dir(cwd) / "latest.json"
    if not path.exists():
        return None
    payload = _sanitize_snapshot_payload(
        json.loads(path.read_text(encoding="utf-8"))
    )
    return _prefer_event_log_messages(cwd, payload)


def list_session_snapshots(cwd: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """List saved sessions for the project, newest first."""
    session_dir = get_project_session_dir(cwd)
    sessions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Named session files
    for path in sorted(session_dir.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("session_id", path.stem.replace("session-", ""))
            seen_ids.add(sid)
            summary = _snapshot_summary(data)
            sessions.append({
                "session_id": sid,
                "summary": summary,
                "message_count": data.get("message_count", len(data.get("messages", []))),
                "model": data.get("model", ""),
                "created_at": data.get("created_at", path.stat().st_mtime),
            })
        except (json.JSONDecodeError, OSError):
            continue
        if len(sessions) >= limit:
            break

    # Also include latest.json if it has no corresponding session file
    latest_path = session_dir / "latest.json"
    if latest_path.exists() and len(sessions) < limit:
        try:
            data = json.loads(latest_path.read_text(encoding="utf-8"))
            sid = data.get("session_id", "latest")
            if sid not in seen_ids:
                summary = _snapshot_summary(data)
                sessions.append({
                    "session_id": sid,
                    "summary": summary or "(latest session)",
                    "message_count": data.get("message_count", len(data.get("messages", []))),
                    "model": data.get("model", ""),
                    "created_at": data.get("created_at", latest_path.stat().st_mtime),
                })
        except (json.JSONDecodeError, OSError):
            pass

    # Sort by created_at descending
    sessions.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return sessions[:limit]


def load_session_by_id(cwd: str | Path, session_id: str) -> dict[str, Any] | None:
    """Load a specific session by ID (snapshot is the legacy fallback)."""
    session_dir = get_project_session_dir(cwd)
    # Try named session first
    path = session_dir / f"session-{session_id}.json"
    if path.exists():
        payload = _sanitize_snapshot_payload(
            json.loads(path.read_text(encoding="utf-8"))
        )
        return _prefer_event_log_messages(cwd, payload)
    # Fallback to latest.json if session_id matches
    latest = session_dir / "latest.json"
    if latest.exists():
        data = _sanitize_snapshot_payload(
            json.loads(latest.read_text(encoding="utf-8"))
        )
        if data.get("session_id") == session_id or session_id == "latest":
            return _prefer_event_log_messages(cwd, data)
    return None


def export_session_markdown(
    *,
    cwd: str | Path,
    messages: list[ConversationMessage],
) -> Path:
    """Export the session transcript as Markdown."""
    session_dir = get_project_session_dir(cwd)
    path = session_dir / "transcript.md"
    parts: list[str] = ["# OpenHarness Session Transcript"]
    for message in messages:
        parts.append(f"\n## {message.role.capitalize()}\n")
        text = message.text.strip()
        if text:
            parts.append(text)
        for tool_use in message.tool_uses:
            parts.append(
                f"\n```tool\n{tool_use.name} {json.dumps(tool_use.input, ensure_ascii=True)}\n```"
            )
        for result_block in message.content:
            if isinstance(result_block, ToolResultBlock):
                parts.append(f"\n```tool-result\n{result_block.content}\n```")
    atomic_write_text(path, "\n".join(parts).strip() + "\n")
    return path
