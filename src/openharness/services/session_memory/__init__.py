"""File-backed session memory for compact continuity."""

from __future__ import annotations

import re
from hashlib import sha1
from pathlib import Path

from openharness.config.paths import get_data_dir
from openharness.domain.messages import ConversationMessage, ToolResultBlock
from openharness.utils.fs import atomic_write_text
from openharness.utils.tokens import estimate_tokens

MAX_SESSION_MEMORY_CHARS = 12_000
MAX_RECENT_LINES = 80


def get_session_memory_dir(cwd: str | Path) -> Path:
    """Return the project session-memory directory."""

    root = Path(cwd).resolve()
    digest = sha1(str(root).encode("utf-8")).hexdigest()[:12]
    path = get_data_dir() / "session-memory" / f"{root.name}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_memory_path(cwd: str | Path, session_id: str | None = None) -> Path:
    """Return the markdown session-memory path."""

    safe_session = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (session_id or "default"))
    return get_session_memory_dir(cwd) / f"{safe_session or 'default'}.md"


def prepare_session_memory_metadata(
    cwd: str | Path,
    tool_metadata: dict[str, object],
    *,
    session_id: str | None = None,
) -> Path:
    """Ensure metadata points compaction to the session-memory file."""

    sid = session_id or str(tool_metadata.get("session_id") or "default")
    path = get_session_memory_path(cwd, sid)
    tool_metadata["session_memory_path"] = str(path)
    return path


def get_session_memory_content(path: str | Path | None) -> str:
    """Read session memory content if available."""

    if not path:
        return ""
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return ""
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def update_session_memory_file(
    cwd: str | Path,
    messages: list[ConversationMessage],
    *,
    tool_metadata: dict[str, object] | None = None,
    session_id: str | None = None,
) -> Path:
    """Update the deterministic session-memory checkpoint."""

    path = prepare_session_memory_metadata(cwd, tool_metadata or {}, session_id=session_id)
    body = build_session_memory_document(messages, tool_metadata=tool_metadata)
    atomic_write_text(path, body)
    return path


def build_session_memory_document(
    messages: list[ConversationMessage],
    *,
    tool_metadata: dict[str, object] | None = None,
) -> str:
    """Build a compact markdown checkpoint for the current session."""

    state = tool_metadata.get("task_focus_state") if isinstance(tool_metadata, dict) else None
    goal = ""
    next_step = ""
    verified: list[str] = []
    artifacts: list[str] = []
    if isinstance(state, dict):
        goal = str(state.get("goal") or "").strip()
        next_step = str(state.get("next_step") or "").strip()
        verified = [str(item).strip() for item in state.get("verified_state", []) if str(item).strip()]
        artifacts = [str(item).strip() for item in state.get("active_artifacts", []) if str(item).strip()]

    lines = ["# Session Memory", ""]
    lines.extend(["## Current State", goal or "(no current goal recorded)", ""])
    if next_step:
        lines.extend(["## Next Step", next_step, ""])
    verification = _verification_lines(tool_metadata)
    if verification:
        lines.extend(["## Verification", *verification, ""])
    if verified:
        # Named "Explored" on purpose: this list is what the session *looked at*
        # (files read, greps run, commands executed).  It used to be titled
        # "Verified Work", which made a hand-off claim a verification that never
        # happened -- and a reader who trusts it stops checking.
        lines.extend(["## Explored", *[f"- {item}" for item in verified[-10:]], ""])
    open_items = _open_checklist_items(tool_metadata)
    if open_items:
        lines.extend(["## Open Items", *[f"- {item}" for item in open_items], ""])
    if artifacts:
        lines.extend(["## Active Artifacts", *[f"- {item}" for item in artifacts[-10:]], ""])
    lines.extend(["## Recent Conversation", *_recent_message_lines(messages), ""])
    text = "\n".join(lines).strip() + "\n"
    if len(text) > MAX_SESSION_MEMORY_CHARS:
        text = text[:MAX_SESSION_MEMORY_CHARS].rsplit("\n", 1)[0]
        text += "\n\n> Session memory was truncated to stay within budget.\n"
    return text


_OPEN_ITEM_RE = re.compile(r"^\s*-\s+\[ \]\s+(.+?)\s*$")


def _verification_lines(tool_metadata: dict[str, object] | None) -> list[str]:
    """What is actually known about verification, in two lines or none.

    Imported lazily: ``services`` is allowed to depend on ``engine``, but the
    engine's session-memory callers already import this module, and a top-level
    import would make that cycle load-order dependent.
    """

    from openharness.engine import verification_ledger

    gate = verification_ledger.status(tool_metadata)
    green = gate.last_green
    if green is None and not gate.needs_verification:
        return []
    lines: list[str] = []
    if green is not None:
        lines.append(f"- last green full run: `{green.command}` (returncode {green.returncode})")
    if gate.needs_verification:
        lines.append(f"- status: UNVERIFIED -- {gate.reason}")
    else:
        lines.append("- status: no edits since that run")
    return lines


def _open_checklist_items(tool_metadata: dict[str, object] | None) -> list[str]:
    """Unchecked entries of the session checklist, so a resume cannot drop them."""

    if not isinstance(tool_metadata, dict):
        return []
    raw_path = tool_metadata.get("plan_file")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return []
    try:
        text = Path(raw_path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for line in text.splitlines():
        match = _OPEN_ITEM_RE.match(line)
        if match is not None:
            items.append(match.group(1))
    return items[:8]


def session_memory_to_compact_text(content: str) -> str:
    """Prepare persisted session memory for insertion across compact boundaries."""

    stripped = content.strip()
    if not stripped:
        return ""
    if estimate_tokens(stripped) > 4_000:
        stripped = stripped[:MAX_SESSION_MEMORY_CHARS].rsplit("\n", 1)[0]
    return "Session memory checkpoint from earlier in this conversation:\n" + stripped


def _recent_message_lines(messages: list[ConversationMessage]) -> list[str]:
    lines: list[str] = []
    for message in messages[-MAX_RECENT_LINES:]:
        line = _summarize_message(message)
        if line:
            lines.append(f"- {line}")
    return lines or ["- (no recent messages)"]


def _summarize_message(message: ConversationMessage) -> str:
    text = " ".join(message.text.split())
    if text:
        return f"{message.role}: {text[:220]}"
    if message.tool_uses:
        return f"{message.role}: tool calls -> {', '.join(block.name for block in message.tool_uses[:6])}"
    if any(isinstance(block, ToolResultBlock) for block in message.content):
        return f"{message.role}: tool results returned"
    return f"{message.role}: [non-text content]"
