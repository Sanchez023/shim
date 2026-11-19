"""Tool for reading task output."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tasks.summary import (
    format_task_summary,
    read_task_output_with_meta,
    truncation_note,
)
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

_TERMINAL_STATUSES = {"completed", "failed", "killed"}


class TaskOutputToolInput(BaseModel):
    """Arguments for task output retrieval."""

    task_id: str = Field(description="Task identifier")
    max_bytes: int = Field(default=12000, ge=1, le=100000)
    block: bool = Field(
        default=False,
        description="Wait for the task to finish before returning its output.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Maximum time to wait when block=true.",
    )


class TaskOutputTool(BaseTool):
    """Read the output of a background task."""

    name = "task_output"
    description = (
        "Read a background task's state and output log: task type, status, "
        "description, elapsed time, progress notes, and the output tail. With "
        "block=true, wait for the task to finish first — use this to collect "
        "a subagent's result."
    )
    input_model = TaskOutputToolInput

    def is_read_only(self, arguments: TaskOutputToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: TaskOutputToolInput, context: ToolExecutionContext) -> ToolResult:
        del context
        manager = get_task_manager()
        timed_out = False

        task = None
        get_task = getattr(manager, "get_task", None)
        if callable(get_task):
            task = get_task(arguments.task_id)

        if arguments.block:
            task = await manager.wait_for_task(
                arguments.task_id,
                timeout_seconds=arguments.timeout_seconds,
            )
            if task is None:
                return ToolResult(
                    output=f"Task not found: {arguments.task_id}",
                    is_error=True,
                )
            timed_out = task.status not in _TERMINAL_STATUSES

        try:
            output, total_chars, truncated = read_task_output_with_meta(
                manager,
                arguments.task_id,
                max_bytes=arguments.max_bytes,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

        parts: list[str] = []
        if task is not None:
            parts.append(format_task_summary(task, task_id=arguments.task_id))
        if truncated:
            parts.append(truncation_note(max_bytes=arguments.max_bytes, total_chars=total_chars))

        body = output
        if not body.strip():
            status = getattr(task, "status", None) if task is not None else None
            if status == "running":
                body = (
                    "(no output yet — the task is still starting or waiting "
                    "on the model; call task_output with block=true to wait)"
                )
            else:
                body = "(no output)"
        parts.append(body)

        if timed_out:
            note = (
                f"[task still running after {arguments.timeout_seconds} seconds; "
                "call task_output again with block=true to keep waiting]"
            )
            parts.append(note)

        metadata: dict[str, object] = {"task_id": arguments.task_id}
        if task is not None:
            metadata["status"] = getattr(task, "status", None)
            metadata["type"] = getattr(task, "type", None)
            metadata["description"] = getattr(task, "description", None)
        metadata["truncated"] = truncated

        return ToolResult(output="\n".join(parts), metadata=metadata)
