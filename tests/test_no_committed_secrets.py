"""Guard against committing live provider credentials.

Runs as part of the normal test suite so a leaked key fails CI instead of
waiting for a human reviewer to notice it.

If this test fails on your branch: revoke the key first, then remove it from
the working tree *and* from history (``git filter-repo --replace-text``).
Deleting the line alone does not undo the leak.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Built by concatenation so this file never matches itself.
PATTERNS = (
    re.compile("sk" + "-[A-Za-z0-9]{32,}"),  # Anthropic / OpenAI / Moonshot style
    re.compile("gh" + "p_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile("AK" + "IA[0-9A-Z]{16}"),  # AWS access key id
    re.compile("xox" + "b-[A-Za-z0-9-]{20,}"),  # Slack bot token
)

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".sh", ".ps1", ".ts", ".tsx", ".js", ".html", ".env", "",
}

# Fixtures that intentionally contain *fake* credentials.
ALLOWED = {
    "tests/test_no_committed_secrets.py",
    "tests/test_memory/test_claude_runtime_memory.py",
}


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        pytest.skip("git not available")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


@pytest.mark.parametrize("rel_path", _tracked_files())
def test_tracked_file_has_no_live_secret(rel_path: str) -> None:
    path = ROOT / rel_path
    if path.suffix.lower() not in TEXT_SUFFIXES or rel_path in ALLOWED:
        return
    if "node_modules" in path.parts or ".venv" in path.parts:
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:  # pragma: no cover - unreadable/binary
        return

    for pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            redacted = match.group(0)[:6] + "…"
            pytest.fail(
                f"possible live credential in {rel_path}: {redacted} "
                f"(offset {match.start()}). Revoke it, remove it, and purge history."
            )
