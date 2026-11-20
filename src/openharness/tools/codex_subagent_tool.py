"""Model-facing tool that delegates one task to Codex app-server."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from openharness.tool_ports import get_default_codex_subagent_port
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class CodexSubagentInput(BaseModel):
    """Arguments for a one-shot Codex delegation."""

    task: str = Field(description="Self-contained task for the external Codex agent")
    model: str | None = Field(
        default=None,
        description="Optional Codex model override; omit to use native Codex settings",
    )


class CodexSubagentTool(BaseTool[CodexSubagentInput]):
    """Delegate a task to a fresh, unattended Codex thread."""

    name = "subagent_codex"
    description = (
        "Delegate a self-contained task to an external Codex agent. "
        "The task runs in a fresh unattended Codex thread and returns only "
        "the final answer or a safe failure diagnostic."
    )
    input_model = CodexSubagentInput

    async def execute(
        self,
        arguments: CodexSubagentInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        port = (
            context.metadata.get("codex_subagent_port")
            or get_default_codex_subagent_port()
        )
        try:
            result = await port.run(
                arguments.task,
                cwd=str(context.cwd),
                model=arguments.model,
            )
        except Exception as exc:  # noqa: BLE001 - normalized into a tool result
            logger.warning("subagent_codex failed: %s", exc)
            return ToolResult(
                output=f"Codex subagent failed: {type(exc).__name__}: {exc}",
                is_error=True,
                metadata={"provider": "codex", "stop_reason": "failed"},
            )

        if result.stop_reason != "completed":
            return ToolResult(
                output=result.diagnostic or result.output or "Codex subagent failed",
                is_error=True,
                metadata={"provider": "codex", "stop_reason": result.stop_reason},
            )
        return ToolResult(
            output=result.output,
            metadata={"provider": "codex", "stop_reason": result.stop_reason},
        )
