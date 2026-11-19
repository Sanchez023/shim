"""Lightweight @-mention file search for the React TUI."""

from __future__ import annotations

import os
from pathlib import Path

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    ".openharness",
}


def list_project_files(cwd: str | Path, *, max_files: int = 5000) -> list[str]:
    """Return project-relative POSIX paths, skipping heavy/vendor directories."""
    root = Path(cwd).resolve()
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".git")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            files.append(rel.as_posix())
            if len(files) >= max_files:
                return files
    return files


def _fuzzy_score(path: str, query: str) -> float | None:
    """Return a lower-is-better score, or None when the query doesn't match."""
    lowered_path = path.lower()
    lowered_query = query.lower()
    if not lowered_query:
        return float(path.count("/")) + len(path) / 10_000

    contiguous = lowered_path.find(lowered_query)
    if contiguous >= 0:
        return contiguous + len(path) * 0.01

    cursor = 0
    positions: list[int] = []
    for index, char in enumerate(lowered_path):
        if cursor < len(lowered_query) and char == lowered_query[cursor]:
            positions.append(index)
            cursor += 1
            if cursor == len(lowered_query):
                break
    if cursor != len(lowered_query):
        return None
    span = positions[-1] - positions[0] + 1
    return span + len(path) * 0.01


def rank_files(paths: list[str], query: str, *, limit: int = 20) -> list[str]:
    """Rank an already-scanned file list for an @ mention."""
    scored: list[tuple[float, str]] = []
    for path in paths:
        score = _fuzzy_score(path, query)
        if score is not None:
            scored.append((score, path))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [path for _, path in scored[: max(1, limit)]]


def search_files(
    cwd: str | Path,
    query: str,
    *,
    limit: int = 20,
    max_files: int = 5000,
) -> list[str]:
    """Fuzzy-search project files for an @ mention."""
    return rank_files(
        list_project_files(cwd, max_files=max_files),
        query,
        limit=limit,
    )
