"""Expand bare ``@path`` mentions into model-visible file context."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from openharness.permissions.checker import (
    SENSITIVE_PATH_PATTERNS,
    _policy_match_paths,
)

_MENTION_PATTERN = re.compile(r"(?<!\S)@([^\s@]+)")
_MAX_FILE_BYTES = 100_000
_MAX_TOTAL_BYTES = 200_000


def _is_sensitive(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    for candidate in _policy_match_paths(normalized):
        for pattern in SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def expand_file_mentions(text: str, cwd: str | Path) -> tuple[str, list[str]]:
    """Append fenced file contents after ``@path`` mentions.

    Returns the expanded text and the list of attached project-relative paths.
    Missing, outside-project, oversized, or credential files are left untouched.
    """
    if "@" not in text:
        return text, []
    root = Path(cwd).resolve()
    expanded = text
    attached: list[str] = []
    seen: set[str] = set()
    total_bytes = 0

    for match in _MENTION_PATTERN.finditer(text):
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        candidate = (root / raw).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file() or _is_sensitive(candidate):
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES or total_bytes + size > _MAX_TOTAL_BYTES:
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total_bytes += size
        attached.append(relative.as_posix())
        expanded += (
            f"\n\n<file path=\"{relative.as_posix()}\">\n"
            f"{content.rstrip()}\n"
            "</file>"
        )

    return expanded, attached
