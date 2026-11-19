"""Tool for spawning local agent tasks."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from openharness.hooks import HookEvent
from openharness.runtime.seams.agents import AgentRunPort, AgentSpawnRequest
from openharness.tasks import get_task_manager
from openharness.tasks.summary import (
    format_duration,
    read_task_output_with_meta,
    task_elapsed_seconds,
    truncation_note,
)
from openharness.tool_ports import get_default_agent_run_port
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class AgentToolInput(BaseModel):
    """Arguments for local agent spawning."""

    description: str = Field(description="Short description of the delegated work")
    prompt: str = Field(description="Full prompt for the local agent")
    subagent_type: str = Field(
        default="worker",
        description=(
            "Agent type for definition lookup: 'Explore' for read-only research, "
            "'Plan' for design, 'worker' for implementation, or 'verification' for checks."
        ),
    )
    model: str | None = Field(default=None)
    command: str | None = Field(default=None, description="Override spawn command")
    team: str | None = Field(default=None, description="Optional team to attach the agent to")
    mode: str = Field(
        default="local_agent",
        description="Agent mode: local_agent, remote_agent, or in_process_teammate",
    )
    wait: bool | None = Field(
        default=None,
        description=(
            "Wait for the agent to finish and return its result. Defaults to "
            "true for Explore/Plan and false for worker/verification."
        ),
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Maximum time to wait when the call blocks.",
    )


_BLOCKING_SUBAGENT_TYPES = frozenset({"explore", "plan"})


def _should_wait(arguments: AgentToolInput) -> bool:
    if arguments.wait is not None:
        return arguments.wait
    return (
        arguments.mode == "local_agent"
        and arguments.subagent_type.strip().lower() in _BLOCKING_SUBAGENT_TYPES
    )


async def _collect_agent_result(
    task_id: str,
    *,
    agent_name: str,
    timeout_seconds: int,
    metadata: dict[str, object],
) -> ToolResult:
    """Wait for a delegated agent and return its output directly."""
    manager = get_task_manager()
    record = await manager.wait_for_task(task_id, timeout_seconds=timeout_seconds)
    if record is None:
        return ToolResult(
            output=f"Spawned agent {agent_name} (task_id={task_id}), but its task record disappeared.",
            is_error=True,
            metadata={**metadata, "status": "unknown", "blocked": True},
        )

    try:
        output, total_chars, truncated = read_task_output_with_meta(
            manager,
            task_id,
            max_bytes=12_000,
        )
    except ValueError as exc:
        return ToolResult(
            output=str(exc),
            is_error=True,
            metadata={**metadata, "status": record.status, "blocked": True},
        )

    timed_out = record.status not in {"completed", "failed", "killed"}
    body = output.strip() or "(no output)"
    elapsed = task_elapsed_seconds(record)
    elapsed_suffix = f", elapsed {format_duration(elapsed)}" if elapsed is not None else ""
    if truncated:
        body = f"{truncation_note(max_bytes=12_000, total_chars=total_chars)}\n\n{body}"
    if timed_out:
        body += (
            f"\n\n[subagent still running after {timeout_seconds}s; "
            f'task_id={task_id}; call task_output(task_id="{task_id}", block=true) to keep waiting]'
        )
        header = (
            f"Subagent {agent_name} is still running "
            f"(task_id={task_id}{elapsed_suffix}):"
        )
    else:
        header = (
            f"Subagent {agent_name} finished with status={record.status} "
            f"(task_id={task_id}{elapsed_suffix}):"
        )
    return ToolResult(
        output=f"{header}\n{body}",
        is_error=record.status == "failed",
        metadata={**metadata, "status": record.status, "blocked": True},
    )


class AgentTool(BaseTool):
    """Spawn a local agent subprocess."""

    name = "agent"
    description = (
        "Delegate independent research, broad exploration, implementation, or "
        "verification to a subagent. Prefer this when a task needs three or more "
        "broad searches/reads or spans multiple files or areas. Explore/Plan "
        "calls wait and return the result directly; worker/verification calls "
        "run in the background. Spawn several agents in one response when the "
        "work is independent."
    )
    input_model = AgentToolInput

    def is_read_only(self, arguments: BaseModel) -> bool:
        """Explore/Plan subagents are read-only research, so plan mode keeps them.

        Their definitions disallow file edits, and the subprocess inherits the
        current permission mode. Worker/verification agents can mutate the
        workspace, so they still require leaving plan mode.
        """
        if not isinstance(arguments, AgentToolInput):
            return False
        return arguments.subagent_type.strip().lower() in _BLOCKING_SUBAGENT_TYPES

    async def execute(self, arguments: AgentToolInput, context: ToolExecutionContext) -> ToolResult:
        if arguments.mode not in {"local_agent", "remote_agent", "in_process_teammate"}:
            return ToolResult(
                output="Invalid mode. Use local_agent, remote_agent, or in_process_teammate.",
                is_error=True,
            )

        port: AgentRunPort = context.agent_run_port or get_default_agent_run_port()

        # Resolve the requested agent definition, falling back to the generic
        # profile so an unknown type still gets the shared agent guidelines.
        agent_def = port.resolve_definition(arguments.subagent_type)
        if agent_def is None:
            agent_def = port.resolve_definition("general-purpose")

        # Resolve team and agent name for the swarm backend. Keep the requested
        # name even when falling back to the generic definition.
        team = arguments.team or "default"
        agent_name = arguments.subagent_type

        request = AgentSpawnRequest(
            name=agent_name,
            team=team,
            prompt=arguments.prompt,
            cwd=str(context.cwd),
            parent_session_id="main",
            model=arguments.model or (agent_def.model if agent_def else None),
            command=arguments.command,
            system_prompt=agent_def.system_prompt if agent_def else None,
            permissions=agent_def.permissions if agent_def else (),
            task_type=arguments.mode,
        )

        try:
            result = await port.spawn(request)
        except Exception as exc:
            logger.error("Failed to spawn agent: %s", exc)
            return ToolResult(output=str(exc), is_error=True)

        if not result.success:
            return ToolResult(output=result.error or "Failed to spawn agent", is_error=True)

        if arguments.team:
            port.attach_to_team(arguments.team, result.task_id)

        if context.hook_executor is not None:
            manager = get_task_manager()
            unregister = None

            async def _emit_subagent_stop(task_record) -> None:
                nonlocal unregister
                if task_record.id != result.task_id:
                    return
                if unregister is not None:
                    unregister()
                    unregister = None
                await context.hook_executor.execute(
                    HookEvent.SUBAGENT_STOP,
                    {
                        "event": HookEvent.SUBAGENT_STOP.value,
                        "agent_id": result.agent_id,
                        "task_id": result.task_id,
                        "backend_type": result.backend_type,
                        "status": task_record.status,
                        "return_code": task_record.return_code,
                        "description": arguments.description,
                        "subagent_type": arguments.subagent_type or "agent",
                        "team": team,
                        "mode": arguments.mode,
                    },
                )

            unregister = manager.register_completion_listener(_emit_subagent_stop)
            task_record = manager.get_task(result.task_id)
            if task_record is not None and task_record.status in {"completed", "failed", "killed"}:
                await _emit_subagent_stop(task_record)

        metadata: dict[str, object] = {
            "agent_id": result.agent_id,
            "task_id": result.task_id,
            "backend_type": result.backend_type,
            "description": arguments.description,
        }
        if _should_wait(arguments):
            return await _collect_agent_result(
                result.task_id,
                agent_name=agent_name,
                timeout_seconds=arguments.timeout_seconds,
                metadata=metadata,
            )

        description = arguments.description.strip()
        description_suffix = f' for "{description}"' if description else ""
        return ToolResult(
            output=(
                f"Spawned agent {result.agent_id}{description_suffix} "
                f"(task_id={result.task_id}, backend={result.backend_type}). "
                f"Continue useful work; read the result with "
                f"task_output(task_id=\"{result.task_id}\", block=true)."
            ),
            metadata={**metadata, "status": "running", "blocked": False},
        )
