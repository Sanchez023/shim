"""Tool for deleting local cron jobs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tool_ports import get_default_cron_port
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronDeleteToolInput(BaseModel):
    """Arguments for deleting a cron job."""

    name: str = Field(description="Cron job name")


class CronDeleteTool(BaseTool):
    """Delete a local cron job."""

    name = "cron_delete"
    description = "Delete a local cron-style job by name."
    input_model = CronDeleteToolInput

    async def execute(
        self,
        arguments: CronDeleteToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        port = context.metadata.get("cron_port") or get_default_cron_port()
        if not port.delete_job(arguments.name):
            return ToolResult(output=f"Cron job not found: {arguments.name}", is_error=True)
        return ToolResult(output=f"Deleted cron job {arguments.name}")
