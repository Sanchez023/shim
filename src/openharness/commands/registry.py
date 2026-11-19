"""Slash command registry.

The built-in command implementations live in :mod:`openharness.commands.builtin`
and the shared helpers in :mod:`openharness.commands.helpers`. This module keeps
the public registry surface (types, lookup, and ``create_default_command_registry``)
and re-exports the legacy helper names for compatibility.
"""

from __future__ import annotations

from collections.abc import Iterable

from openharness.commands.builtin.memory import register_memory_commands
from openharness.commands.builtin.models import register_models_commands
from openharness.commands.builtin.session import register_session_commands
from openharness.commands.builtin.settings import register_settings_commands
from openharness.commands.builtin.workspace import register_workspace_commands
from openharness.commands.helpers import (
    _coerce_setting_value,
    _copy_to_clipboard,
    _is_secret_key,
    _is_valid_skill_command_name,
    _last_message_text,
    _make_skill_slash_command,
    _redact_config_value,
    _register_user_invocable_skill_commands,
    _render_plugin_command_prompt,
    _render_skill_command_prompt,
    _rewind_turns,
    _run_git_command,
    _settings_json_for_display,
    _shorten_text,
    _skill_command_handler,
    _skill_command_name,
    lookup_skill_slash_command,
)
from openharness.commands.memory import (
    _handle_memory_agent_command,
    _handle_memory_edit_command,
    _handle_memory_session_command,
    _handle_memory_team_command,
    _handle_memory_validate_command,
    _memory_backend_for_context,
    _parse_memory_add_flags,
    _resolve_memory_candidate,
    _resolve_memory_entry_path,
)
from openharness.commands.types import (
    CommandContext,
    CommandHandler,
    CommandResult,
    MemoryCommandBackend,
    SlashCommand,
)
from openharness.plugins.types import PluginCommandDefinition

__all__ = [
    "CommandContext",
    "CommandHandler",
    "CommandRegistry",
    "CommandResult",
    "MemoryCommandBackend",
    "SlashCommand",
    "_coerce_setting_value",
    "_copy_to_clipboard",
    "_handle_memory_agent_command",
    "_handle_memory_edit_command",
    "_handle_memory_session_command",
    "_handle_memory_team_command",
    "_handle_memory_validate_command",
    "_is_secret_key",
    "_is_valid_skill_command_name",
    "_last_message_text",
    "_make_skill_slash_command",
    "_memory_backend_for_context",
    "_parse_memory_add_flags",
    "_redact_config_value",
    "_register_user_invocable_skill_commands",
    "_render_plugin_command_prompt",
    "_render_skill_command_prompt",
    "_resolve_memory_candidate",
    "_resolve_memory_entry_path",
    "_rewind_turns",
    "_run_git_command",
    "_settings_json_for_display",
    "_shorten_text",
    "_skill_command_handler",
    "_skill_command_name",
    "create_default_command_registry",
    "lookup_skill_slash_command",
]


class CommandRegistry:
    """Map slash commands to handlers."""

    def __init__(self) -> None:
        # Primary commands keyed by canonical name, plus aliases pointing at
        # the same SlashCommand instance. We keep a separate set of canonical
        # names so help/listing output doesn't duplicate aliased entries.
        self._commands: dict[str, SlashCommand] = {}
        self._canonical_names: list[str] = []

    def register(self, command: SlashCommand) -> None:
        """Register a command, plus any aliases pointing at the same handler."""
        if command.name not in self._commands:
            self._canonical_names.append(command.name)
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    def lookup(self, raw_input: str) -> tuple[SlashCommand, str] | None:
        """Parse a slash command and return its handler plus raw args."""
        if not raw_input.startswith("/"):
            return None
        name, _, args = raw_input[1:].partition(" ")
        command = self._commands.get(name)
        if command is None:
            return None
        return command, args.strip()

    def help_text(self) -> str:
        """Return a formatted summary of all registered commands."""
        lines = ["Available commands:"]
        commands = [self._commands[name] for name in self._canonical_names]
        for command in sorted(commands, key=lambda item: item.name):
            lines.append(f"/{command.name:<12} {command.description}")
        return "\n".join(lines)

    def list_commands(self) -> list[SlashCommand]:
        """Return canonical commands in registration order (aliases omitted)."""
        return [self._commands[name] for name in self._canonical_names]


def create_default_command_registry(
    plugin_commands: Iterable[PluginCommandDefinition] | None = None,
) -> CommandRegistry:
    """Create the built-in command registry."""
    registry = CommandRegistry()
    register_session_commands(registry)
    register_memory_commands(registry)
    register_settings_commands(registry)
    register_workspace_commands(registry)
    register_models_commands(registry)

    for plugin_command in plugin_commands or ():
        if not plugin_command.user_invocable:
            continue

        async def _plugin_command_handler(
            args: str,
            context: CommandContext,
            *,
            command: PluginCommandDefinition = plugin_command,
        ) -> CommandResult:
            prompt = _render_plugin_command_prompt(
                command,
                args,
                getattr(context, "session_id", None),
            )
            if command.disable_model_invocation:
                return CommandResult(message=prompt)
            return CommandResult(
                submit_prompt=prompt,
                submit_model=command.model,
            )

        registry.register(
            SlashCommand(
                plugin_command.name,
                plugin_command.description,
                _plugin_command_handler,
            )
        )
    return registry
