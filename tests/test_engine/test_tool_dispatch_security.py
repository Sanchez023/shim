"""SecurityRequest must carry the raw tool input to policy delegates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from openharness.config.settings import PermissionSettings
from openharness.engine.tool_dispatch import execute_tool_call
from openharness.permissions.checker import PermissionChecker
from openharness.runtime.seams.governance.models import (
    SecurityDecision,
    SecurityRequest,
)
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult


class _EchoInput(BaseModel):
    value: str


class _EchoTool(BaseTool):
    name = "echo"
    description = "echo"
    input_model = _EchoInput

    async def execute(self, arguments: _EchoInput, context: ToolExecutionContext) -> ToolResult:
        del context
        return ToolResult(output=arguments.value)


class _CapturePolicy:
    def __init__(self) -> None:
        self.requests: list[SecurityRequest] = []

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        self.requests.append(request)
        return SecurityDecision(allowed=True, dimension="test")


@pytest.mark.asyncio
async def test_security_request_receives_raw_tool_input(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    policy = _CapturePolicy()
    context = SimpleNamespace(
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        hook_executor=None,
        permission_prompt=None,
        ask_user_prompt=None,
        activate_tools=None,
        approval_gate=None,
        security_policy=policy,
        tool_metadata={},
        event_sink=None,
        event_sequencer=None,
        turn_id="turn",
        current_step_id="step",
        tool_executor=None,
    )

    result = await execute_tool_call(context, "echo", "toolu_1", {"value": "hello"})

    assert result.content == "hello"
    assert len(policy.requests) == 1
    assert policy.requests[0].tool_input == {"value": "hello"}
