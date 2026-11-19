"""Agent orchestration port (ADR-0010 D2).

Leaf tools describe *what* to run; backend and team-registry selection lives
behind this port, implemented in ``openharness.services``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentDefinitionView:
    """The subset of an agent definition a leaf tool needs."""

    model: str | None = None
    system_prompt: str | None = None
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpawnRequest:
    """A backend-neutral request to spawn one delegated agent."""

    name: str
    team: str
    prompt: str
    cwd: str
    parent_session_id: str = "main"
    model: str | None = None
    command: str | None = None
    system_prompt: str | None = None
    permissions: tuple[str, ...] = field(default_factory=tuple)
    task_type: str = "local_agent"


@dataclass(frozen=True)
class AgentSpawnResult:
    """Backend-neutral spawn outcome."""

    success: bool
    task_id: str = ""
    agent_id: str = ""
    backend_type: str = ""
    error: str = ""


@runtime_checkable
class AgentRunPort(Protocol):
    """Spawn, message, and team-attach delegated agents."""

    def resolve_definition(self, subagent_type: str) -> AgentDefinitionView | None:
        """Return the definition for a subagent type, if one exists."""

    async def spawn(self, request: AgentSpawnRequest) -> AgentSpawnResult:
        """Spawn one agent through the configured backend."""

    async def send_message(self, agent_id: str, message: str) -> None:
        """Send a follow-up message to a swarm agent."""

    def attach_to_team(self, team: str, task_id: str) -> None:
        """Attach a spawned task to a team, creating the team if needed."""

    def create_team(self, name: str, description: str = "") -> str:
        """Create a team and return its name."""

    def delete_team(self, name: str) -> None:
        """Delete a team."""
