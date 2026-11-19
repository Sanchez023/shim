"""Minimal application state model."""

from __future__ import annotations

#: Shapes the TUI can give the todo/plan panel.  Shared by `/todos`, the
#: `set_ui_preference` request and the React frontend.
TODO_PANEL_MODES: tuple[str, ...] = ("full", "compact", "hidden")

from dataclasses import dataclass, field


@dataclass
class AppState:
    """Shared mutable UI/session state."""

    model: str
    permission_mode: str
    theme: str
    cwd: str = "."
    provider: str = "unknown"
    auth_status: str = "missing"
    base_url: str = ""
    vim_enabled: bool = False
    voice_enabled: bool = False
    voice_available: bool = False
    voice_reason: str = ""
    fast_mode: bool = False
    effort: str = "medium"
    passes: int = 1
    personality: str = "shim"
    tool_profile: str = "core"
    mcp_connected: int = 0
    mcp_failed: int = 0
    bridge_sessions: int = 0
    output_style: str = "default"
    # Which shape the TUI gives the todo/plan panel: full | compact | hidden.
    todo_panel: str = "full"
    keybindings: dict[str, str] = field(default_factory=dict)
