"""Tool for leaving plan permission mode."""

from __future__ import annotations

from pydantic import BaseModel

from openharness.config.settings import load_settings, save_settings
from openharness.permissions import PermissionMode
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ExitPlanModeToolInput(BaseModel):
    """No-op input model."""


class ExitPlanModeTool(BaseTool):
    """Leave plan mode and resume with full-auto permissions.

    The agent-initiated exit goes to auto so implementation can continue
    without a prompt for every edit; users who want the confirm-everything
    mode switch to Default manually (Shift+Tab or /permissions).
    """

    name = "exit_plan_mode"
    description = (
        "Leave plan mode and switch permission mode to full auto. Use this when "
        "the plan is ready and the user wants implementation; the user is asked "
        "to confirm the switch."
    )
    input_model = ExitPlanModeToolInput

    async def execute(self, arguments: ExitPlanModeToolInput, context: ToolExecutionContext) -> ToolResult:
        del arguments
        set_mode = context.metadata.get("set_permission_mode")
        if callable(set_mode):
            set_mode(PermissionMode.FULL_AUTO.value)
        else:
            settings = load_settings()
            settings.permission.set_mode(PermissionMode.FULL_AUTO)
            save_settings(settings)
        return ToolResult(output="Permission mode set to auto")
