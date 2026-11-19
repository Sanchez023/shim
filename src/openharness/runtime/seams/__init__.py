"""Protocol definitions grouped by capability domain.

Exports are resolved on first access; see :mod:`openharness.runtime._lazy` for
why eager re-exports here would create an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openharness.runtime._lazy import make_getattr

if TYPE_CHECKING:
    from openharness.api.capabilities import ModelCapabilities
    from openharness.runtime.seams.core import (
        EventLogPort,
        EventLogStore,
        EventSink,
        EventSubscriber,
        Projection,
        SessionAppendPort,
    )
    from openharness.runtime.seams.execution import (
        ApprovalGate,
        ModelClient,
        ToolExecutor,
        model_capabilities,
    )

__all__ = [
    "AgentBackend",
    "AgentCapabilities",
    "ApprovalGate",
    "CallableApprovalGate",
    "EventLogPort",
    "EventLogStore",
    "EventSink",
    "EventSubscriber",
    "LocalPythonBackend",
    "LocalToolExecutor",
    "ModelCapabilities",
    "ModelClient",
    "Projection",
    "SessionAppendPort",
    "SubagentManager",
    "SubagentProvider",
    "SubagentResult",
    "SubagentStartRequest",
    "ToolExecutor",
    "model_capabilities",
]

_SPEC: dict[str, tuple[str, str]] = {
    "AgentBackend": ("openharness.services.agent_backend", "AgentBackend"),
    "AgentCapabilities": ("openharness.services.agent_backend", "AgentCapabilities"),
    "LocalPythonBackend": ("openharness.services.agent_backend", "LocalPythonBackend"),
    "SubagentManager": ("openharness.services.subagent_provider", "SubagentManager"),
    "SubagentProvider": ("openharness.services.subagent_provider", "SubagentProvider"),
    "SubagentResult": ("openharness.services.subagent_provider", "SubagentResult"),
    "SubagentStartRequest": ("openharness.services.subagent_provider", "SubagentStartRequest"),
    "EventLogPort": ("openharness.runtime.seams.core", "EventLogPort"),
    "EventLogStore": ("openharness.runtime.seams.core", "EventLogStore"),
    "EventSink": ("openharness.runtime.seams.core", "EventSink"),
    "EventSubscriber": ("openharness.runtime.seams.core", "EventSubscriber"),
    "Projection": ("openharness.runtime.seams.core", "Projection"),
    "SessionAppendPort": ("openharness.runtime.seams.core", "SessionAppendPort"),
    "ApprovalGate": ("openharness.runtime.seams.execution", "ApprovalGate"),
    "CallableApprovalGate": ("openharness.engine.tool_executor", "CallableApprovalGate"),
    "LocalToolExecutor": ("openharness.engine.tool_executor", "LocalToolExecutor"),
    "ModelClient": ("openharness.runtime.seams.execution", "ModelClient"),
    "ModelCapabilities": ("openharness.api.capabilities", "ModelCapabilities"),
    "ToolExecutor": ("openharness.runtime.seams.execution", "ToolExecutor"),
    "model_capabilities": ("openharness.runtime.seams.execution", "model_capabilities"),
}

__getattr__ = make_getattr(__name__, _SPEC)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
