"""Shared slash-command types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from openharness.engine.query_engine import QueryEngine
from openharness.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend

if TYPE_CHECKING:
    from openharness.domain.messages import ConversationMessage
    from openharness.state import AppStateStore
    from openharness.tools.base import ToolRegistry


@dataclass
class CommandResult:
    """Result returned by a slash command."""

    message: str | None = None
    should_exit: bool = False
    clear_screen: bool = False
    replay_messages: list[ConversationMessage] | None = None  # replayed in the TUI
    continue_pending: bool = False
    continue_turns: int | None = None
    refresh_runtime: bool = False
    submit_prompt: str | None = None
    submit_model: str | None = None


@dataclass(frozen=True)
class MemoryCommandBackend:
    """Storage backend used by the generic ``/memory`` slash command."""

    label: str
    default_type: str
    default_category: str
    get_memory_dir: Callable[[], Path]
    get_entrypoint: Callable[[], Path]
    list_files: Callable[[], list[Path]]
    add_entry: Callable[[str, str], Path]
    remove_entry: Callable[[str], bool]


@dataclass
class CommandContext:
    """Context available to command handlers."""

    engine: QueryEngine
    hooks_summary: str = ""
    mcp_summary: str = ""
    plugin_summary: str = ""
    cwd: str = "."
    tool_registry: ToolRegistry | None = None
    app_state: AppStateStore | None = None
    session_backend: SessionBackend = DEFAULT_SESSION_BACKEND
    session_id: str | None = None
    extra_skill_dirs: Iterable[str | Path] | None = None
    extra_plugin_roots: Iterable[str | Path] | None = None
    memory_backend: MemoryCommandBackend | None = None
    include_project_memory: bool = True
    # CLI overrides applied to every ``current_settings()`` read. Slash
    # commands remove the entry for a key they change so the new value is not
    # shadowed by the startup flag.
    settings_overrides: dict[str, object] | None = None
    # Async transcript printer.  Commands that stream background output (such
    # as ``/agents follow``) use it after they return; ``None`` in contexts
    # without a transcript (tests, programmatic calls).
    print_system: Callable[[str], Awaitable[None]] | None = None


CommandHandler = Callable[[str, CommandContext], Awaitable[CommandResult]]


@dataclass
class SlashCommand:
    """Definition of a slash command."""

    name: str
    description: str
    handler: CommandHandler
    remote_invocable: bool = True
    remote_admin_opt_in: bool = False
    aliases: tuple[str, ...] = ()
    #: Read-only commands may run while a turn is streaming, instead of being
    #: queued behind it. Only set this when the handler never mutates the
    #: conversation, the engine turn, the runtime, or settings.
    busy_safe: bool = False
