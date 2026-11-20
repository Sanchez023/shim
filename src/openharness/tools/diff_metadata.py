"""Diff payload attached to edit-tool results for the terminal UI."""

from __future__ import annotations

# The inline diff is only a preview, but the full transcript and the approval
# modal consume the same payload.  Cap pathological replace-all edits so one
# event cannot carry a multi-megabyte string.
MAX_DIFF_METADATA_CHARS = 120_000

# Above this combined size, skip the whole-file unified diff (only used when no
# approval prompt will page through it).  The UI falls back to a diff of the
# tool's old/new snippets, which is cheap and still line-accurate.
MAX_DIFF_SOURCE_CHARS = 2_000_000


def diff_metadata(diff_text: str, added: int, removed: int) -> dict[str, object]:
    """Return the ``ToolResult.metadata`` entry describing an edit."""
    if len(diff_text) > MAX_DIFF_METADATA_CHARS:
        diff_text = (
            diff_text[:MAX_DIFF_METADATA_CHARS].rsplit("\n", 1)[0]
            + "\n… diff truncated\n"
        )
    return {
        "diff": diff_text,
        "diff_added": added,
        "diff_removed": removed,
    }


__all__ = [
    "MAX_DIFF_METADATA_CHARS",
    "MAX_DIFF_SOURCE_CHARS",
    "diff_metadata",
]
