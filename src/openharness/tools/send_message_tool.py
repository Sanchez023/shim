"""Tool for writing messages to running agent tasks."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tool_ports import get_default_agent_run_port
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class SendMessageToolInput(BaseModel):
    """Arguments for sending a follow-up message to a task."""

    task_id: str = Field(description="Target local agent task id or swarm agent_id (name@team)")
    message: str = Field(description="Message to write to the task stdin")


class SendMessageTool(BaseTool):
    """Send a message to a running local agent task."""

    name = "send_message"
    description = "Send a follow-up message to a running local agent task."
    input_model = SendMessageToolInput

    async def execute(self, arguments: SendMessageToolInput, context: ToolExecutionContext) -> ToolResult:
        # Swarm agents use agent_id format (name@team); legacy tasks use plain task IDs
        if "@" in arguments.task_id:
            return await self._send_swarm_message(
                arguments.task_id, arguments.message, context
            )
        try:
            await get_task_manager().write_to_task(arguments.task_id, arguments.message)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to task {arguments.task_id}")

    async def _send_swarm_message(
        self,
        agent_id: str,
        message: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Route a message to a swarm agent via the orchestration port."""
        port = context.agent_run_port or get_default_agent_run_port()
        try:
            await port.send_message(agent_id, message)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except Exception as exc:
            logger.error("Failed to send message to %s: %s", agent_id, exc)
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to agent {agent_id}")
