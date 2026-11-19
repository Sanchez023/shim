"""Tests for tool visibility profiles and deferred tool loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.tools import create_default_tool_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.profiles import (
    DEFERRED_BUILTIN_TOOLS,
    visible_tool_names,
)
from openharness.tools.tool_search_tool import ToolSearchTool, ToolSearchToolInput


def test_core_profile_hides_only_deferred_builtins() -> None:
    registry = create_default_tool_registry()

    visible = visible_tool_names(registry, "core")
    assert visible is not None
    assert "tool_search" in visible
    assert "bash" in visible
    assert "read_file" in visible
    assert "agent" in visible
    assert "image_generation" not in visible
    assert "cron_create" not in visible
    assert "task_update" not in visible
    assert DEFERRED_BUILTIN_TOOLS <= {tool.name for tool in registry.list_tools()}


def test_loaded_tools_become_visible() -> None:
    registry = create_default_tool_registry()

    visible = visible_tool_names(registry, "core", loaded={"image_generation"})

    assert visible is not None
    assert "image_generation" in visible
    assert "cron_create" not in visible


def test_full_profile_returns_none() -> None:
    registry = create_default_tool_registry()
    assert visible_tool_names(registry, "full") is None


@pytest.mark.asyncio
async def test_tool_search_activates_matches(tmp_path: Path) -> None:
    registry = create_default_tool_registry()
    activated: list[list[str]] = []

    result = await ToolSearchTool().execute(
        ToolSearchToolInput(query="image_generation"),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={
                "tool_registry": registry,
                "activate_tools": lambda names: activated.append(list(names)),
            },
        ),
    )

    assert "image_generation" in result.output
    assert activated and activated[0] == ["image_generation"]
    assert result.metadata["tool_names"] == ["image_generation"]


@pytest.mark.asyncio
async def test_tool_search_caps_activations(tmp_path: Path) -> None:
    registry = create_default_tool_registry()
    activated: list[list[str]] = []

    result = await ToolSearchTool().execute(
        ToolSearchToolInput(query="task"),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={
                "tool_registry": registry,
                "activate_tools": lambda names: activated.append(list(names)),
            },
        ),
    )

    assert activated and len(activated[0]) == 5
    assert len(result.metadata["tool_names"]) == 5
    assert result.metadata["skipped_tool_names"]
    assert "Matched but not activated" in result.output


@pytest.mark.asyncio
async def test_tool_search_activation_limit_is_configurable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENHARNESS_TOOL_SEARCH_MAX_ACTIVATIONS", "2")
    registry = create_default_tool_registry()
    activated: list[list[str]] = []

    await ToolSearchTool().execute(
        ToolSearchToolInput(query="task"),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={
                "tool_registry": registry,
                "activate_tools": lambda names: activated.append(list(names)),
            },
        ),
    )

    assert activated and len(activated[0]) == 2


@pytest.mark.asyncio
async def test_tool_search_ranks_exact_name_first(tmp_path: Path) -> None:
    registry = create_default_tool_registry()
    activated: list[list[str]] = []

    result = await ToolSearchTool().execute(
        ToolSearchToolInput(query="task_update"),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={
                "tool_registry": registry,
                "activate_tools": lambda names: activated.append(list(names)),
            },
        ),
    )

    assert activated[0][0] == "task_update"
    assert result.metadata["tool_names"][0] == "task_update"


@pytest.mark.asyncio
async def test_tool_search_rejects_empty_query(tmp_path: Path) -> None:
    registry = create_default_tool_registry()
    activated: list[list[str]] = []

    result = await ToolSearchTool().execute(
        ToolSearchToolInput(query="   "),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={
                "tool_registry": registry,
                "activate_tools": lambda names: activated.append(list(names)),
            },
        ),
    )

    assert result.is_error is True
    assert "at least two characters" in result.output
    assert activated == []
