"""Tool for maintaining the session checklist or a project TODO file."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.fs import write_text_preserving_newlines

_CHECKLIST_RE = re.compile(r"^(\s*-\s+\[)([ xX])(\]\s+)(.*)$")

#: How similar a "new" item has to be to an existing open one before the write is
#: treated as a status update instead of a second checklist entry.  A model that
#: restates an item slightly differently ("起草 ADR-0012" ->
#: "起草 ADR-0012 与 0013") used to append a ticked duplicate while the original
#: stayed unchecked forever.
_SIMILARITY_THRESHOLD = 0.9


def _item_key(text: str) -> str:
    """Identity key for an item: whitespace and punctuation are not meaningful.

    Collapsing whitespace alone was not enough: a model that writes
    ``落地 截断修复`` for ``落地截断修复`` still missed.  Dropping separators
    entirely (CJK ideographs are word characters, so they are kept) makes the
    match independent of how the text was re-typed.
    """
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).casefold()


def _find_checklist_line(
    existing: str,
    item: str,
) -> tuple[str | None, re.Match[str] | None]:
    """Find a checklist line for *item*, tolerating case/spacing/format drift."""
    target = _item_key(item)
    for line in existing.splitlines():
        match = _CHECKLIST_RE.match(line)
        if match is not None and _item_key(match.group(4)) == target:
            return line, match
    return None, None


def _find_similar_checklist_line(
    existing: str,
    item: str,
) -> tuple[str | None, re.Match[str] | None]:
    """Find an existing entry that is clearly the same item worded differently.

    Only used when the exact/normalised match failed and the caller is about to
    append, so a genuine second item is never merged into the first.  Numbers
    are treated as meaningful: ``ADR-0012`` and ``ADR-0013`` differ by a digit
    but are different work.
    """
    target = _item_key(item)
    if len(target) < 4:
        return None, None
    target_numbers = re.findall(r"\d+", target)
    best: tuple[float, str, re.Match[str]] | None = None
    for line in existing.splitlines():
        match = _CHECKLIST_RE.match(line)
        if match is None:
            continue
        candidate = _item_key(match.group(4))
        if len(candidate) < 4:
            continue
        if target in candidate or candidate in target:
            score = 1.0
        else:
            if re.findall(r"\d+", candidate) != target_numbers:
                continue
            score = difflib.SequenceMatcher(None, target, candidate).ratio()
            if score < _SIMILARITY_THRESHOLD:
                continue
        if best is None or score > best[0]:
            best = (score, line, match)
    if best is None:
        return None, None
    return best[1], best[2]


def _open_items(markdown: str) -> list[str]:
    """Text of every unchecked checklist entry, in file order."""
    return [
        match.group(4)
        for line in markdown.splitlines()
        if (match := _CHECKLIST_RE.match(line)) is not None and match.group(2) == " "
    ]


def _open_items_suffix(markdown: str) -> str:
    """A short reminder of what is still open, appended to the tool result.

    A tool result is the only thing the model reliably reads back, so listing
    the leftovers is what actually closes the loop; without it the checklist
    silently accumulated stale entries.
    """
    open_items = _open_items(markdown)
    if not open_items:
        return " (all items done)"
    shown = ", ".join(f'"{name}"' for name in open_items[:3])
    if len(open_items) > 3:
        shown += f", +{len(open_items) - 3} more"
    return f" ({len(open_items)} still open: {shown})"



class TodoWriteToolInput(BaseModel):
    """Arguments for TODO writes."""

    item: str = Field(description="TODO item text")
    checked: bool = Field(default=False)
    path: str | None = Field(
        default=None,
        description=(
            "Optional project checklist file. Omit to use the session checklist "
            "shown in the sidebar; plan mode always uses the session plan."
        ),
    )


class TodoWriteTool(BaseTool):
    """Add or update an item in a markdown checklist."""

    name = "todo_write"
    description = (
        "Add a new checklist item or mark an existing one as done, and update "
        "its status in the sidebar. Uses the session checklist by default; pass "
        "`path` to write a project file instead."
    )
    input_model = TodoWriteToolInput

    def _resolve_path(
        self,
        arguments: TodoWriteToolInput,
        context: ToolExecutionContext,
    ) -> Path:
        item = arguments.item.strip()
        plan_mode = str(context.metadata.get("permission_mode") or "").strip().lower() == "plan"
        plan_file = str(context.metadata.get("plan_file") or "").strip()
        plan_path = Path(plan_file) if plan_file else None

        if plan_mode and plan_path is not None:
            # Plan checklists belong to the session. Ignore the requested path
            # so plan mode can never mutate the repository through this tool.
            return plan_path

        # Status updates must land in the file that actually holds the item.
        # Otherwise leaving plan mode makes "mark done" append to TODO.md while
        # the sidebar keeps showing the session plan.
        if plan_path is not None and plan_path.exists():
            existing = plan_path.read_text(encoding="utf-8", errors="replace")
            if _find_checklist_line(existing, item)[0] is not None:
                return plan_path

        if arguments.path:
            return Path(context.cwd) / arguments.path
        if plan_path is not None:
            return plan_path
        return Path(context.cwd) / "TODO.md"

    async def execute(self, arguments: TodoWriteToolInput, context: ToolExecutionContext) -> ToolResult:
        item = arguments.item.strip()
        if not item:
            return ToolResult(output="todo_write requires a non-empty item", is_error=True)

        path = self._resolve_path(arguments, context)
        path.parent.mkdir(parents=True, exist_ok=True)

        plan_mode = str(context.metadata.get("permission_mode") or "").strip().lower() == "plan"
        existing = (
            path.read_text(encoding="utf-8")
            if path.exists()
            else ("# Plan\n" if plan_mode or path.name.startswith("plan-") else "# TODO\n")
        )

        desired = "x" if arguments.checked else " "
        target_line = f"- [{desired}] {item}"

        # Match the checklist item irrespective of surrounding whitespace or
        # capitalization; models often restate the item slightly differently
        # when marking it done. The original text is preserved, only the
        # checkbox flips.
        existing_line, existing_match = _find_checklist_line(existing, item)
        matched_note = ""
        if existing_line is None:
            similar_line, similar_match = _find_similar_checklist_line(existing, item)
            if similar_line is not None and similar_match is not None:
                existing_line, existing_match = similar_line, similar_match
                matched_note = f' (matched existing item "{similar_match.group(4)}")'
        if existing_line is not None and existing_match is not None:
            current = existing_match.group(2)
            if current.lower() == "x" and not arguments.checked:
                desired = " "
            elif current == " " and arguments.checked:
                desired = "x"
            else:
                return ToolResult(
                    output=f"No change needed in {path}{_open_items_suffix(existing)}",
                    metadata={"path": str(path)},
                )
            replacement = (
                f"{existing_match.group(1)}{desired}{existing_match.group(3)}"
                f"{existing_match.group(4)}"
            )
            updated = existing.replace(existing_line, replacement, 1)
        else:
            # New item — append
            updated = existing.rstrip() + f"\n{target_line}\n"

        write_text_preserving_newlines(path, updated)
        return ToolResult(
            output=f"Updated {path}{matched_note}{_open_items_suffix(updated)}",
            metadata={"path": str(path)},
        )
