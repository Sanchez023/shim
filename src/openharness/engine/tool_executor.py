"""Default execution seams backed by the engine's tool pipeline.

These adapters live beside the engine because they call ``tool_dispatch``;
the protocols remain in :mod:`openharness.runtime.seams.execution`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from openharness.domain.messages import ToolResultBlock
from openharness.engine.tool_dispatch import execute_tool_call


class LocalToolExecutor:
    """Default executor backed by the in-process permission/tool pipeline."""

    async def execute(
        self,
        context: Any,
        tool_name: str,
        tool_use_id: str,
        tool_input: dict[str, object],
    ) -> ToolResultBlock:
        return await execute_tool_call(context, tool_name, tool_use_id, tool_input)


class CallableApprovalGate:
    """Adapt the legacy ``permission_prompt`` callback to a seam provider."""

    def __init__(self, prompt: Callable[[str, str], Awaitable[bool]]) -> None:
        self._prompt = prompt

    async def request(
        self,
        *,
        tool_name: str,
        reason: str,
        context: Any | None = None,
    ) -> bool:
        del context
        return await self._prompt(tool_name, reason)
