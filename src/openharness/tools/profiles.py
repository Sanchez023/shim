"""Tool visibility profiles and on-demand schema loading.

The full registry is always executable. The ``core`` profile sends only the
everyday coding tools to the model and hides niche built-ins; ``tool_search``
enables matching tools for subsequent requests. This keeps the fixed tool
schema payload (about 6.5k tokens for 39 built-ins) much smaller.
"""

from __future__ import annotations

from collections.abc import Iterable

from openharness.tools.base import ToolRegistry

FULL_PROFILE = "full"
CORE_PROFILE = "core"
DEFAULT_TOOL_PROFILE = CORE_PROFILE

# Built-ins that are only loaded after an explicit tool_search match. Plugin
# and MCP tools are not in this set, so user extensions stay visible.
DEFERRED_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "image_generation",
        "image_to_text",
        "notebook_edit",
        "lsp",
        "mcp_auth",
        "cron_create",
        "cron_list",
        "cron_delete",
        "cron_toggle",
        "remote_trigger",
        "task_create",
        "task_get",
        "task_list",
        "task_stop",
        "task_output",
        "task_update",
        "team_create",
        "team_delete",
        "enter_worktree",
        "exit_worktree",
        "config",
        "brief",
        "sleep",
    }
)

ALWAYS_VISIBLE_TOOLS: frozenset[str] = frozenset({"tool_search"})


def normalize_tool_profile(value: str | None) -> str:
    """Normalize a configured profile name."""
    normalized = (value or "").strip().lower()
    if normalized in {"", "full", "all"}:
        return FULL_PROFILE if normalized else DEFAULT_TOOL_PROFILE
    return CORE_PROFILE


def visible_tool_names(
    registry: ToolRegistry,
    profile: str,
    *,
    loaded: Iterable[str] = (),
) -> set[str] | None:
    """Return visible tool names, or ``None`` for the full profile."""
    normalized = normalize_tool_profile(profile)
    if normalized == FULL_PROFILE:
        return None
    loaded_names = set(loaded)
    return {
        tool.name
        for tool in registry.list_tools()
        if tool.name not in DEFERRED_BUILTIN_TOOLS
        or tool.name in ALWAYS_VISIBLE_TOOLS
        or tool.name in loaded_names
    }


__all__ = [
    "ALWAYS_VISIBLE_TOOLS",
    "CORE_PROFILE",
    "DEFAULT_TOOL_PROFILE",
    "DEFERRED_BUILTIN_TOOLS",
    "FULL_PROFILE",
    "normalize_tool_profile",
    "visible_tool_names",
]
