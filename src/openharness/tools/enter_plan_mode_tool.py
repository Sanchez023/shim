"""Tool for entering plan permission mode."""

from __future__ import annotations

from pydantic import BaseModel

from openharness.config.settings import load_settings, save_settings
from openharness.permissions import PermissionMode
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class EnterPlanModeToolInput(BaseModel):
    """No-op input model."""


class EnterPlanModeTool(BaseTool):
    """Switch settings permission mode to plan."""

    name = "enter_plan_mode"
    description = (
        "Switch permission mode to plan (read-only planning). Use this when the "
        "user asks for a plan, design review, or analysis that must not modify "
        "the workspace; call exit_plan_mode when the plan is ready to implement."
    )
    input_model = EnterPlanModeToolInput

    async def execute(self, arguments: EnterPlanModeToolInput, context: ToolExecutionContext) -> ToolResult:
        del arguments
        set_mode = context.metadata.get("set_permission_mode")
        if callable(set_mode):
            set_mode(PermissionMode.PLAN.value)
        else:
            settings = load_settings()
            settings.permission.set_mode(PermissionMode.PLAN)
            save_settings(settings)
        return ToolResult(output="Permission mode set to plan")
