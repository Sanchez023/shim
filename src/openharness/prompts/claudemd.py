"""Project instruction discovery (Claude, Codex/DSH, and Cursor conventions)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from openharness.prompts.active_files import active_files_match_globs

_CLAUDE_FILES = ("CLAUDE.md", Path(".claude") / "CLAUDE.md")
_AGENTS_FILES = ("AGENTS.md",)
_CURSOR_LEGACY = (".cursorrules",)


def discover_project_rule_files(cwd: str | Path) -> list[Path]:
    """Discover project instruction files from the cwd upward.

    Supports the Claude convention (``CLAUDE.md`` / ``.claude/rules``), the
    Codex/DSH convention (``AGENTS.md``), and Cursor's ``.cursor/rules/*.mdc``
    and legacy ``.cursorrules``.
    """
    current = Path(cwd).resolve()
    results: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path.exists() and path.is_file() and path not in seen:
            results.append(path)
            seen.add(path)

    for directory in [current, *current.parents]:
        for candidate in _CLAUDE_FILES:
            add(directory / candidate)
        for candidate in _AGENTS_FILES:
            add(directory / candidate)
        for candidate in _CURSOR_LEGACY:
            add(directory / candidate)

        claude_rules = directory / ".claude" / "rules"
        if claude_rules.is_dir():
            for rule in sorted(claude_rules.glob("*.md")):
                add(rule)

        cursor_rules = directory / ".cursor" / "rules"
        if cursor_rules.is_dir():
            for rule in sorted(cursor_rules.glob("*.mdc")):
                add(rule)
            for rule in sorted(cursor_rules.glob("*.md")):
                add(rule)

        if directory.parent == directory:
            break

    return results


def _parse_cursor_rule(path: Path) -> tuple[dict[str, object], str]:
    """Split an optional YAML frontmatter block from a Cursor rule body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, parts[2].lstrip("\n")


def load_project_rules_prompt_parts(
    cwd: str | Path,
    *,
    max_chars_per_file: int = 12_000,
    max_total_chars: int = 30_000,
    active_files: Iterable[str] | None = None,
) -> tuple[str | None, str | None]:
    """Load project instructions split into stable and active-file scoped parts.

    The stable part contains always-apply rules (plain instruction files and
    ``alwaysApply: true`` Cursor rules).  The scoped part contains glob-keyed
    Cursor rules that depend on the current turn's active files.  Callers put
    the stable part inside the cached prefix and the scoped part at the tail,
    so an ``@file`` turn does not rewrite the stable prefix.
    """
    files = discover_project_rule_files(cwd)
    if not files:
        return None, None

    stable_lines = ["# Project Instructions"]
    scoped_lines = ["# Project Instructions (Scoped To Active Files)"]
    stable_used = 0
    scoped_used = 0
    for path in files:
        metadata: dict[str, object] = {}
        if path.suffix.lower() == ".mdc":
            metadata, content = _parse_cursor_rule(path)
        else:
            content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + "\n...[truncated]..."

        globs = metadata.get("globs")
        glob_list = (
            [globs]
            if isinstance(globs, str) and globs.strip()
            else [str(glob) for glob in globs]
            if isinstance(globs, list) and globs
            else []
        )
        always_apply = metadata.get("alwaysApply") is True
        is_scoped = bool(glob_list) and not always_apply
        if (
            is_scoped
            and active_files is not None
            and not active_files_match_globs(active_files, glob_list, cwd)
        ):
            continue

        heading = f"## {path}"
        description = metadata.get("description")
        if isinstance(description, str) and description.strip():
            heading += f" — {description.strip()}"
        block_lines = ["", heading]
        if glob_list:
            block_lines.append(
                "> Cursor rule scope: globs=" + ", ".join(glob_list)
            )
        elif always_apply:
            block_lines.append("> Cursor rule scope: alwaysApply")
        block_lines.extend(["```md", content.strip(), "```"])
        block = "\n".join(block_lines)
        lines = scoped_lines if is_scoped else stable_lines
        used = scoped_used if is_scoped else stable_used
        if used + len(block) > max_total_chars:
            lines.extend(["", heading, "_[omitted by prompt budget]_"])
            continue
        if is_scoped:
            scoped_used += len(block)
        else:
            stable_used += len(block)
        lines.append(block)

    stable = "\n".join(stable_lines) if len(stable_lines) > 1 else None
    scoped = "\n".join(scoped_lines) if len(scoped_lines) > 1 else None
    return stable, scoped


def load_project_rules_prompt(
    cwd: str | Path,
    *,
    max_chars_per_file: int = 12_000,
    max_total_chars: int = 30_000,
    active_files: Iterable[str] | None = None,
) -> str | None:
    """Load discovered instruction files into one prompt section.

    Cursor rules with ``globs`` and ``alwaysApply: false`` are included only
    when one of the turn's active files matches a glob.  Passing
    ``active_files=None`` keeps the legacy behavior and includes every rule.
    """
    stable, scoped = load_project_rules_prompt_parts(
        cwd,
        max_chars_per_file=max_chars_per_file,
        max_total_chars=max_total_chars,
        active_files=active_files,
    )
    parts = [part for part in (stable, scoped) if part]
    return "\n\n".join(parts) if parts else None


# Backward-compatible names used by older callers/tests.
discover_claude_md_files = discover_project_rule_files


def load_claude_md_prompt(cwd: str | Path, *, max_chars_per_file: int = 12_000) -> str | None:
    return load_project_rules_prompt(cwd, max_chars_per_file=max_chars_per_file)
