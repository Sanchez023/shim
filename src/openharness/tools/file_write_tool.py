"""File writing tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.diff_metadata import MAX_DIFF_SOURCE_CHARS, diff_metadata
from openharness.utils.fs import write_text_preserving_newlines


class FileWriteToolInput(BaseModel):
    """Arguments for the file write tool."""

    path: str = Field(description="Path of the file to write")
    content: str = Field(description="Full file contents")
    create_directories: bool = Field(default=True)


class FileWriteTool(BaseTool):
    """Write complete file contents."""

    name = "write_file"
    description = "Create or overwrite a text file in the local repository."
    input_model = FileWriteToolInput

    async def execute(
        self,
        arguments: FileWriteToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        path = _resolve_path(context.cwd, arguments.path)

        from openharness.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from openharness.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        try:
            original = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            # The write can still proceed; the diff simply starts from scratch.
            original = ""
        approval_prompt = context.metadata.get("edit_approval_prompt") if context.metadata else None
        # The terminal renders the unified diff under the tool row, so compute
        # it even when there is no approval prompt.  Skip pathologically large
        # sources unless the approval modal needs them.
        if (
            approval_prompt is not None
            or len(original) + len(arguments.content) <= MAX_DIFF_SOURCE_CHARS
        ):
            diff_text, added, removed = _compute_diff(
                str(path), original, arguments.content
            )
        else:
            diff_text, added, removed = "", 0, 0
        metadata = diff_metadata(diff_text, added, removed)
        if approval_prompt is not None:
            reply = await approval_prompt(str(path), diff_text, added, removed)
            if reply == "reject":
                return ToolResult(output=f"Write rejected by user: {path}", is_error=True)
            if arguments.create_directories:
                path.parent.mkdir(parents=True, exist_ok=True)
            write_text_preserving_newlines(path, arguments.content)
            stats = f"  ({_ANSI_GREEN}+{added}{_ANSI_RESET} {_ANSI_RED}-{removed}{_ANSI_RESET})"
            return ToolResult(output=f"Wrote {path}{stats}", metadata=metadata)

        if arguments.create_directories:
            path.parent.mkdir(parents=True, exist_ok=True)
        write_text_preserving_newlines(path, arguments.content)
        return ToolResult(output=f"Wrote {path}", metadata=metadata)


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
