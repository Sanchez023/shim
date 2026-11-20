"""String-based file editing tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.diff_metadata import MAX_DIFF_SOURCE_CHARS, diff_metadata
from openharness.utils.fs import write_text_preserving_newlines


class FileEditToolInput(BaseModel):
    """Arguments for the file edit tool."""

    path: str = Field(description="Path of the file to edit")
    old_str: str = Field(description="Existing text to replace")
    new_str: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False)


class FileEditTool(BaseTool[FileEditToolInput]):
    """Replace text in an existing file."""

    name = "edit_file"
    description = "Edit an existing file by replacing a string."
    input_model = FileEditToolInput

    async def execute(
        self,
        arguments: FileEditToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        path = _resolve_path(context.cwd, arguments.path)

        from openharness.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from openharness.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)

        original = path.read_text(encoding="utf-8")
        if arguments.old_str not in original:
            return ToolResult(output="old_str was not found in the file", is_error=True)

        if arguments.replace_all:
            updated = original.replace(arguments.old_str, arguments.new_str)
        else:
            updated = original.replace(arguments.old_str, arguments.new_str, 1)

        approval_prompt = context.metadata.get("edit_approval_prompt") if context.metadata else None
        # Computed even when there is no approval prompt: the terminal renders
        # the same unified diff under the tool row.  A huge file is skipped
        # unless the approval modal needs it; the UI then diffs the snippets.
        if (
            approval_prompt is not None
            or len(original) + len(updated) <= MAX_DIFF_SOURCE_CHARS
        ):
            diff_text, added, removed = _compute_diff(str(path), original, updated)
        else:
            diff_text, added, removed = "", 0, 0
        metadata = diff_metadata(diff_text, added, removed)

        if approval_prompt is not None:
            reply = await approval_prompt(str(path), diff_text, added, removed)
            if reply == "reject":
                return ToolResult(output=f"Edit rejected by user: {path}", is_error=True)
            write_text_preserving_newlines(path, updated)
            stats = f"  ({_ANSI_GREEN}+{added}{_ANSI_RESET} {_ANSI_RED}-{removed}{_ANSI_RESET})"
            return ToolResult(output=f"Updated {path}{stats}", metadata=metadata)

        write_text_preserving_newlines(path, updated)
        return ToolResult(output=f"Updated {path}", metadata=metadata)


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _compute_diff(filename: str, original: str, updated: str) -> tuple[str, int, int]:
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=filename,
            tofile=filename,
            lineterm="",
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return "".join(diff_lines), added, removed


_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"
