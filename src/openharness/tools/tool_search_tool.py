"""Tool for searching available tools."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

MAX_TOOL_ACTIVATIONS = 5


def _activation_limit() -> int:
    """Return how many tools one search may enable.

    Every activated tool is added to the schema of every later request, so a
    broad search permanently grows the prompt — and invalidates the prompt
    cache. The cap keeps one careless query from activating half the registry.
    """
    raw = os.environ.get("OPENHARNESS_TOOL_SEARCH_MAX_ACTIVATIONS", "").strip()
    if not raw:
        return MAX_TOOL_ACTIVATIONS
    try:
        return max(1, int(raw))
    except ValueError:
        return MAX_TOOL_ACTIVATIONS


def _match_rank(name: str, query: str) -> int:
    """Prefer exact and name-prefix matches over description-only matches."""
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    return 3


class ToolSearchToolInput(BaseModel):
    """Arguments for tool search."""

    query: str = Field(description="Substring to search in tool names and descriptions")


class ToolSearchTool(BaseTool):
    """Search tool registry contents and enable matching tools."""

    name = "tool_search"
    description = (
        "Search available tools by name or description. In the core tool "
        "profile, up to five matching tools are enabled for subsequent turns; "
        "use a specific name or phrase to activate the tool you actually need."
    )
    input_model = ToolSearchToolInput

    def is_read_only(self, arguments: ToolSearchToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: ToolSearchToolInput, context: ToolExecutionContext) -> ToolResult:
        registry = context.metadata.get("tool_registry") if hasattr(context, "metadata") else None
        if registry is None:
            return ToolResult(output="Tool registry context not available", is_error=True)
        query = arguments.query.lower().strip()
        if len(query) < 2:
            return ToolResult(
                output=(
                    "Provide at least two characters of a tool name or "
                    "description to search for."
                ),
                is_error=True,
            )
        matches = [
            tool for tool in registry.list_tools()
            if query in tool.name.lower() or query in tool.description.lower()
        ]
        if not matches:
            return ToolResult(output="(no matches)")

        matches.sort(key=lambda tool: (_match_rank(tool.name.lower(), query), tool.name))
        limit = _activation_limit()
        to_activate = [tool.name for tool in matches[:limit]]
        skipped = [tool.name for tool in matches[limit:]]

        activate = context.metadata.get("activate_tools")
        if callable(activate):
            activate(to_activate)

        lines = [
            f"{tool.name}: {tool.description}"
            for tool in matches[:limit]
        ]
        lines.extend(
            [
                "",
                "These tools are now enabled for subsequent turns: "
                + ", ".join(to_activate),
            ]
        )
        if skipped:
            lines.append(
                f"Matched but not activated ({len(skipped)}): "
                + ", ".join(skipped)
                + ". Search a specific name to activate one of them."
            )
        return ToolResult(
            output="\n".join(lines),
            metadata={"tool_names": to_activate, "skipped_tool_names": skipped},
        )
