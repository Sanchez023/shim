"""Collect the files a turn is working on and match Cursor-style globs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_MENTION_RE = re.compile(r"(?<!\S)@([^\s@]+)")
_PATH_RE = re.compile(
    r"(?<!\S)((?:[A-Za-z]:)?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+)"
    r"(?=$|[\s,;:)\]}\"'`])"
)
_FILE_TOKEN_RE = re.compile(r"(?<!\S)([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?=$|[\s,;:)\]}\"'`])")


def _clean_token(token: str) -> str:
    return token.strip().strip("\"'`").rstrip(".,;:)]}")


def _tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _MENTION_RE.finditer(text):
        token = _clean_token(match.group(1))
        if token:
            tokens.append(token)
    for pattern in (_PATH_RE, _FILE_TOKEN_RE):
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if not token or token.startswith(("http://", "https://")):
                continue
            tokens.append(token)
    return tokens


def prompt_mentions_files(prompt: str | None) -> bool:
    """Return True when a user prompt carries file context worth re-filtering."""
    if not prompt:
        return False
    return bool(_tokens_from_text(prompt))


def metadata_has_active_files(metadata: dict[str, Any] | None) -> bool:
    """Return True when session metadata already tracks a working file."""
    if not isinstance(metadata, dict):
        return False
    if metadata.get("read_file_state"):
        return True
    focus = metadata.get("task_focus_state")
    return bool(
        isinstance(focus, dict)
        and isinstance(focus.get("active_artifacts"), list)
        and focus["active_artifacts"]
    )


def collect_active_files(
    prompt: str | None,
    metadata: dict[str, Any] | None = None,
) -> set[str]:
    """Collect relative/absolute file paths implied by a turn."""
    files: set[str] = set()
    if prompt:
        files.update(_tokens_from_text(prompt))
    if not isinstance(metadata, dict):
        return files

    read_state = metadata.get("read_file_state")
    if isinstance(read_state, list):
        for entry in read_state:
            if isinstance(entry, dict):
                path = entry.get("path")
                if isinstance(path, str) and path.strip():
                    files.add(path.strip())

    focus = metadata.get("task_focus_state")
    if isinstance(focus, dict):
        artifacts = focus.get("active_artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, str):
                    continue
                text = item.strip()
                if not text:
                    continue
                # Entries are often "kind: path"; keep the path-like suffix too.
                files.add(text)
                if ":" in text:
                    files.add(text.split(":", 1)[1].strip())

    work_log = metadata.get("recent_work_log")
    if isinstance(work_log, list):
        for entry in work_log:
            if isinstance(entry, str):
                files.update(_tokens_from_text(entry))

    return {path for path in files if path}


def normalize_active_path(path: str, cwd: str | Path) -> str:
    """Normalize a path to a POSIX project-relative path when possible."""
    candidate = Path(path).expanduser()
    root = Path(cwd).resolve()
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            return candidate.as_posix()
    return candidate.as_posix().lstrip("./")


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    expanded: list[str] = []
    for choice in match.group(1).split(","):
        expanded.extend(_expand_braces(f"{prefix}{choice}{suffix}"))
    return expanded


def _glob_to_regex(pattern: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
                    output.append("(?:.*/)?")
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
            index += 1
            continue
        if char == "?":
            output.append("[^/]")
            index += 1
            continue
        if char in ".+()[]{}^$|\\":
            output.append("\\" + char)
        else:
            output.append(char)
        index += 1
    return "".join(output)


def matches_globs(path: str, globs: Iterable[str], cwd: str | Path) -> bool:
    """Match a project-relative path against Cursor-style glob patterns."""
    normalized = normalize_active_path(path, cwd)
    for glob in globs:
        for expanded in _expand_braces(str(glob)):
            if re.fullmatch(_glob_to_regex(expanded.lstrip("./")), normalized):
                return True
    return False


def active_files_match_globs(
    active_files: Iterable[str] | None,
    globs: Iterable[str],
    cwd: str | Path,
) -> bool:
    if active_files is None:
        return True
    return any(matches_globs(path, globs, cwd) for path in active_files)
