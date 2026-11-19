"""Shared helpers used by built-in slash commands."""

from __future__ import annotations

import json
import subprocess
from types import UnionType
from typing import TYPE_CHECKING, Literal, Union, get_args, get_origin

import pyperclip

from openharness.commands.types import CommandContext, CommandResult, SlashCommand
from openharness.config.paths import get_data_dir
from openharness.config.settings import Settings
from openharness.domain.messages import ConversationMessage
from openharness.plugins.types import PluginCommandDefinition
from openharness.skills import load_skill_registry
from openharness.skills.types import SkillDefinition

if TYPE_CHECKING:
    from openharness.commands.registry import CommandRegistry


def _run_git_command(cwd: str, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return False, "git is not installed."
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return False, output or f"git {' '.join(args)} failed"
    return True, output


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        pyperclip.copy(text)
        return True, "clipboard"
    except Exception:  # noqa: BLE001 - platform clipboard backends vary widely
        for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard"]):
            try:
                subprocess.run(command, input=text, text=True, encoding="utf-8", errors="replace", check=True, capture_output=True)
                return True, "clipboard"
            except (OSError, subprocess.SubprocessError):
                continue
    fallback = get_data_dir() / "last_copy.txt"
    fallback.write_text(text, encoding="utf-8")
    return False, str(fallback)


def _last_message_text(messages: list[ConversationMessage]) -> str:
    for message in reversed(messages):
        if message.text.strip():
            return message.text.strip()
    return ""


def _shorten_text(text: str, *, limit: int = 160) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _rewind_turns(messages: list[ConversationMessage], turns: int) -> list[ConversationMessage]:
    updated = list(messages)
    for _ in range(max(0, turns)):
        if not updated:
            break
        while updated:
            popped = updated.pop()
            if popped.role == "user" and popped.text.strip():
                break
    return updated



_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "credential",
    "private_key",
)

_REDACTED = "[REDACTED]"


def _is_secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    if any(part in normalized for part in _SECRET_KEY_PARTS):
        return True
    # A bare "token" marker is wrong for counts: ``max_tokens`` and
    # ``context_window_tokens`` are plurals, credential fields are not
    # (``bot_token``, ``X-Token``, ``auth_token``).
    return normalized == "token" or normalized.endswith("_token")


def _redact_config_value(value: object, *, key: object | None = None) -> object:
    if key is not None and _is_secret_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {item_key: _redact_config_value(item_value, key=item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_config_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_config_value(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("bearer ", "basic ")):
        return _REDACTED
    return value


def _settings_json_for_display(settings: Settings) -> str:
    return json.dumps(_redact_config_value(settings.model_dump()), indent=2, default=str)


def _coerce_setting_value(settings: Settings, key: str, raw: str):
    field = Settings.model_fields.get(key)
    if field is None:
        raise KeyError(key)
    return _coerce_annotation_value(field.annotation, key, raw)


def _strip_optional(annotation: object) -> object:
    """Unwrap ``Optional[X]`` / ``X | None`` so scalar coercion sees ``X``."""
    resolved = annotation
    while True:
        origin = get_origin(resolved)
        if origin is Union or origin is UnionType:
            members = [member for member in get_args(resolved) if member is not type(None)]
            if len(members) != 1:
                return resolved
            resolved = members[0]
            continue
        return resolved


def _coerce_annotation_value(annotation: object, key: str, raw: str):
    """Coerce a CLI string into the declared type of *annotation*.

    The declared type is authoritative: inferring from the current value breaks
    for optional fields, where ``None`` says nothing about the real type and
    ``8192`` used to be stored as the string ``"8192"``.
    """
    resolved = _strip_optional(annotation)
    if resolved is bool:
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {key}: {raw}")
    if resolved is int:
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError(f"Invalid integer value for {key}: {raw}") from None
    if resolved is float:
        try:
            return float(raw.strip())
        except ValueError:
            raise ValueError(f"Invalid number for {key}: {raw}") from None
    if resolved is str:
        return raw
    if get_origin(resolved) is list:
        return [entry.strip() for entry in raw.split(",") if entry.strip()]
    if resolved is Literal or get_origin(resolved) is Literal:
        allowed = get_args(resolved)
        if raw not in allowed:
            raise ValueError(f"Invalid value for {key}: {raw}")
        return raw
    return raw


def _render_plugin_command_prompt(command: PluginCommandDefinition, args: str, session_id: str | None = None) -> str:
    prompt = command.content
    raw_args = args.strip()
    if command.is_skill and command.base_dir:
        prompt = f"Base directory for this skill: {command.base_dir}\n\n{prompt}"
        prompt = prompt.replace("${CLAUDE_SKILL_DIR}", command.base_dir)
    prompt = prompt.replace("${ARGUMENTS}", raw_args).replace("$ARGUMENTS", raw_args)
    if session_id:
        prompt = prompt.replace("${CLAUDE_SESSION_ID}", session_id)
    if raw_args and "${ARGUMENTS}" not in command.content and "$ARGUMENTS" not in command.content:
        prompt = f"{prompt}\n\nArguments: {raw_args}"
    return prompt


def _render_skill_command_prompt(skill: SkillDefinition, args: str, session_id: str | None = None) -> str:
    prompt = skill.content
    raw_args = args.strip()
    if skill.base_dir:
        prompt = f"Base directory for this skill: {skill.base_dir}\n\n{prompt}"
        prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.base_dir)
    prompt = prompt.replace("${ARGUMENTS}", raw_args).replace("$ARGUMENTS", raw_args)
    if session_id:
        prompt = prompt.replace("${CLAUDE_SESSION_ID}", session_id)
    if raw_args and "${ARGUMENTS}" not in skill.content and "$ARGUMENTS" not in skill.content:
        prompt = f"{prompt}\n\nArguments: {raw_args}"
    return prompt


def _skill_command_name(skill: SkillDefinition) -> str:
    return skill.command_name or skill.name


def _is_valid_skill_command_name(name: str) -> bool:
    return bool(name) and not any(char.isspace() for char in name)


async def _skill_command_handler(args: str, context: CommandContext, *, skill_name: str) -> CommandResult:
    skill_registry = load_skill_registry(
        context.cwd,
        extra_skill_dirs=context.extra_skill_dirs,
        extra_plugin_roots=context.extra_plugin_roots,
    )
    skill = skill_registry.get(skill_name)
    if skill is None:
        return CommandResult(message=f"Skill not found: {skill_name}", refresh_runtime=True)
    if not skill.user_invocable:
        return CommandResult(
            message=(
                f"This skill can only be invoked by the model, not directly by users. "
                f"Ask the model to use the {skill_name!r} skill for you."
            )
        )
    prompt = _render_skill_command_prompt(skill, args, getattr(context, "session_id", None))
    return CommandResult(submit_prompt=prompt, submit_model=skill.model)


def _make_skill_slash_command(skill_name: str, description: str) -> SlashCommand:
    return SlashCommand(
        skill_name,
        description,
        lambda args, context, skill_name=skill_name: _skill_command_handler(
            args,
            context,
            skill_name=skill_name,
        ),
    )


def lookup_skill_slash_command(raw_input: str, context: CommandContext) -> tuple[SlashCommand, str] | None:
    """Resolve a user-invocable skill slash command for the active context.

    This is a runtime fallback for skills that are only visible after the
    active cwd, ohmo workspace, or plugin roots are known. Unknown slash
    commands still fall through to the normal agent prompt path.
    """
    if not raw_input.startswith("/"):
        return None
    name, _, args = raw_input[1:].partition(" ")
    name = name.strip()
    if not _is_valid_skill_command_name(name):
        return None
    skill_registry = load_skill_registry(
        context.cwd,
        extra_skill_dirs=context.extra_skill_dirs,
        extra_plugin_roots=context.extra_plugin_roots,
    )
    skill = skill_registry.get(name)
    if skill is None or not skill.user_invocable:
        return None
    command_name = _skill_command_name(skill)
    if not _is_valid_skill_command_name(command_name):
        return None
    return _make_skill_slash_command(command_name, f"Invoke the {command_name} skill"), args.strip()


def _register_user_invocable_skill_commands(registry: CommandRegistry) -> None:
    """Register loaded skills as slash commands.

    Skills are loaded at command execution time because the active command
    context supplies cwd, ohmo extra skill dirs, and plugin roots.
    """

    for skill in load_skill_registry().list_skills():
        if not skill.user_invocable:
            continue
        command_name = _skill_command_name(skill)
        if not _is_valid_skill_command_name(command_name):
            continue
        if registry.lookup(f"/{command_name}") is not None:
            continue
        registry.register(
            _make_skill_slash_command(command_name, f"Invoke the {command_name} skill")
        )
