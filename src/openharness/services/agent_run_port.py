"""Service-backed implementation of the agent orchestration port.

Coordinator/swarm imports are function-local so this adapter always resolves
the current module objects (tests intentionally evict ``openharness.swarm.*``
from ``sys.modules``) and so loading the service does not import swarm.
"""

from __future__ import annotations

from openharness.runtime.seams.agents import (
    AgentDefinitionView,
    AgentSpawnRequest,
    AgentSpawnResult,
)


class ServiceAgentRunPort:
    """Route agent requests to coordinator/swarm implementations."""

    def resolve_definition(self, subagent_type: str) -> AgentDefinitionView | None:
        from openharness.coordinator.agent_definitions import get_agent_definition

        definition = get_agent_definition(subagent_type)
        if definition is None:
            return None
        return AgentDefinitionView(
            model=definition.model,
            system_prompt=definition.system_prompt,
            permissions=tuple(definition.permissions),
        )

    async def spawn(self, request: AgentSpawnRequest) -> AgentSpawnResult:
        from openharness.swarm.registry import get_backend_registry
        from openharness.swarm.types import TeammateSpawnConfig

        executor = get_backend_registry().get_executor("subprocess")
        result = await executor.spawn(
            TeammateSpawnConfig(
                name=request.name,
                team=request.team,
                prompt=request.prompt,
                cwd=request.cwd,
                parent_session_id=request.parent_session_id,
                model=request.model,
                command=request.command,
                system_prompt=request.system_prompt,
                permissions=list(request.permissions),
                task_type=request.task_type,
            )
        )
        return AgentSpawnResult(
            success=bool(result.success),
            task_id=getattr(result, "task_id", "") or "",
            agent_id=getattr(result, "agent_id", "") or "",
            backend_type=getattr(result, "backend_type", "") or "",
            error=getattr(result, "error", "") or "",
        )

    async def send_message(self, agent_id: str, message: str) -> None:
        from openharness.swarm.registry import get_backend_registry
        from openharness.swarm.types import TeammateMessage

        executor = get_backend_registry().get_executor("subprocess")
        await executor.send_message(
            agent_id, TeammateMessage(text=message, from_agent="coordinator")
        )

    def attach_to_team(self, team: str, task_id: str) -> None:
        from openharness.coordinator.coordinator_mode import get_team_registry

        registry = get_team_registry()
        try:
            registry.add_agent(team, task_id)
        except ValueError:
            registry.create_team(team)
            registry.add_agent(team, task_id)

    def create_team(self, name: str, description: str = "") -> str:
        from openharness.coordinator.coordinator_mode import get_team_registry

        return get_team_registry().create_team(name, description).name

    def delete_team(self, name: str) -> None:
        from openharness.coordinator.coordinator_mode import get_team_registry

        get_team_registry().delete_team(name)
