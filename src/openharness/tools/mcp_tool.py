"""MCP tool adapters."""

from __future__ import annotations

import re
from typing import Any, cast

from pydantic import BaseModel, Field, create_model

from openharness.mcp.client import (
    McpClientManager,
    McpServerNotConnectedError,
    McpToolOutput,
)
from openharness.mcp.types import McpToolInfo
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpToolAdapter(BaseTool[BaseModel]):
    """Expose one MCP tool as a normal OpenHarness tool."""

    def __init__(self, manager: McpClientManager, tool_info: McpToolInfo) -> None:
        self._manager = manager
        self._tool_info = tool_info
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"
        self.description = tool_info.description or f"MCP tool {tool_info.name}"
        self.input_model = _input_model_from_schema(self.name, tool_info.input_schema)

    def is_read_only(self, arguments: BaseModel) -> bool:
        del arguments
        return self._tool_info.read_only_hint

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del context
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                arguments.model_dump(mode="json", exclude_none=True),
            )
        except McpServerNotConnectedError as exc:
            return ToolResult(output=str(exc), is_error=True)
        if isinstance(output, McpToolOutput):
            return ToolResult(output=output.text, images=list(output.images))
        # Managers that predate ``McpToolOutput`` (tests, plugins) return text.
        return ToolResult(output=output)


_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _input_model_from_schema(tool_name: str, schema: dict[str, object]) -> type[BaseModel]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return create_model(f"{tool_name.title()}Input")

    required_raw = schema.get("required", [])
    required: set[str] = (
        {str(item) for item in required_raw} if isinstance(required_raw, list) else set()
    )
    fields: dict[str, Any] = {}
    for key in properties:
        key = str(key)
        raw_prop = properties[key]
        prop: dict[str, object] = raw_prop if isinstance(raw_prop, dict) else {}
        py_type = _JSON_TYPE_MAP.get(str(prop.get("type", "")), object)
        if key in required:
            fields[key] = (py_type, Field(default=...))
        else:
            fields[key] = (py_type | None, Field(default=None))
    model = create_model(f"{tool_name.title().replace('-', '_')}Input", **fields)
    return cast(type[BaseModel], model)


def _sanitize_tool_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
