"""Tool schema ordering must be append-only within a session."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from openharness.config.settings import Settings
from openharness.engine.query_engine import QueryEngine
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult


class _EmptyInput(BaseModel):
    pass


class _Tool(BaseTool):
    input_model = _EmptyInput

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"

    async def execute(self, arguments, context) -> ToolResult:
        return ToolResult(output="")


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_Tool(name))
    return registry


def test_append_order_only_moves_tools_to_the_tail():
    registry = _registry("a", "b", "c")

    plain = [item["name"] for item in registry.to_api_schema({"a", "b", "c"})]
    appended = [
        item["name"]
        for item in registry.to_api_schema(
            {"a", "b", "c"}, append_order=("c", "b")
        )
    ]

    assert plain == ["a", "b", "c"]
    assert appended == ["a", "c", "b"]


def test_activation_appends_deferred_tools_without_reordering_existing():
    registry = _registry("bash", "tool_search", "notebook_edit", "lsp")
    engine = QueryEngine(
        api_client=object(),  # type: ignore[arg-type]
        tool_registry=registry,
        permission_checker=PermissionChecker(Settings().permission),
        cwd=Path("."),
        model="deepseek-chat",
        system_prompt="SYSTEM",
        settings=Settings(tool_profile="core"),
    )

    before = [item["name"] for item in engine.visible_tool_schemas()]
    engine.activate_tools(["notebook_edit"])
    after_one = [item["name"] for item in engine.visible_tool_schemas()]
    engine.activate_tools(["lsp"])
    after_two = [item["name"] for item in engine.visible_tool_schemas()]

    assert before == ["bash", "tool_search"]
    assert after_one == ["bash", "tool_search", "notebook_edit"]
    assert after_two == ["bash", "tool_search", "notebook_edit", "lsp"]
    # Activating an already-visible tool must not move it.
    engine.activate_tools(["bash"])
    assert [item["name"] for item in engine.visible_tool_schemas()] == after_two


@pytest.mark.asyncio
async def test_step_runner_sends_the_appended_tool_order(tmp_path: Path):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage, TextBlock

    class _Client:
        def __init__(self) -> None:
            self.tool_orders: list[list[str]] = []

        async def stream_message(self, request):
            self.tool_orders.append([item["name"] for item in request.tools])
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant", content=[TextBlock(text="ok")]
                ),
                usage=UsageSnapshot(input_tokens=10, output_tokens=1),
                stop_reason="stop",
            )

    registry = _registry("bash", "tool_search", "notebook_edit", "lsp")
    client = _Client()
    engine = QueryEngine(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(Settings().permission),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="SYSTEM",
        settings=Settings(tool_profile="core"),
    )
    engine.activate_tools(["notebook_edit"])

    [event async for event in engine.submit_message("hi")]

    assert client.tool_orders == [["bash", "tool_search", "notebook_edit"]]


def test_default_registry_schema_is_deterministic():
    from openharness.tools import create_default_tool_registry

    first = create_default_tool_registry()
    second = create_default_tool_registry()

    assert first.to_api_schema() == second.to_api_schema()
