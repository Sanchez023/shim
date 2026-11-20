"""Runtime settings, permissions, and appearance slash commands."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from openharness.api.provider import auth_status, detect_provider
from openharness.auth.manager import AuthManager
from openharness.commands.helpers import _coerce_setting_value, _settings_json_for_display
from openharness.commands.types import CommandContext, CommandResult, SlashCommand
from openharness.config.paths import get_config_dir, get_feedback_log_path, get_project_config_dir
from openharness.config.settings import Settings, load_settings, save_settings
from openharness.domain.messages import ConversationMessage, ReasoningBlock, ThinkingBlock
from openharness.memory import get_project_memory_dir
from openharness.output_styles import load_output_styles
from openharness.permissions import PermissionMode
from openharness.personality import (
    DEFAULT_PERSONALITY,
    PERSONALITIES,
    list_personalities,
    resolve_personality,
)
from openharness.prompts import build_stable_system_prompt_result
from openharness.state.app_state import TODO_PANEL_MODES
from openharness.version import __version__

if TYPE_CHECKING:
    from openharness.commands.registry import CommandRegistry


def register_settings_commands(registry: CommandRegistry) -> None:
    """Register the built-in settings slash commands."""

    _MODE_LABELS = {"default": "Default", "plan": "Plan Mode", "full_auto": "Auto"}

    async def _config_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        settings = load_settings()
        tokens = args.split(maxsplit=2)
        if not tokens or tokens[0] == "show":
            return CommandResult(message=_settings_json_for_display(settings))
        if tokens[0] == "set" and len(tokens) == 3:
            key, value = tokens[1], tokens[2]
            if key not in Settings.model_fields:
                return CommandResult(message=f"Unknown config key: {key}")
            try:
                coerced = _coerce_setting_value(settings, key, value)
            except ValueError as exc:
                return CommandResult(message=str(exc))
            setattr(settings, key, coerced)
            save_settings(settings)
            return CommandResult(message=f"Updated {key}")
        return CommandResult(message="Usage: /config [show|set KEY VALUE]")

    async def _login_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        settings = load_settings()
        manager = AuthManager(settings)
        profile_name, profile = settings.resolve_profile()
        provider = detect_provider(settings)
        api_key = args.strip()
        if not api_key:
            masked = (
                f"{settings.api_key[:6]}...{settings.api_key[-4:]}"
                if settings.api_key
                else "(not configured)"
            )
            return CommandResult(
                message=(
                    f"Auth status:\n"
                    f"- profile: {profile_name}\n"
                    f"- provider: {provider.name}\n"
                    f"- auth_source: {profile.auth_source}\n"
                    f"- auth_status: {auth_status(settings)}\n"
                    f"- base_url: {settings.base_url or '(default)'}\n"
                    f"- model: {settings.model}\n"
                    f"- api_key: {masked}\n"
                    "Usage: /login API_KEY"
                )
            )
        manager.store_profile_credential(profile_name, "api_key", api_key)
        return CommandResult(message="Stored API key in ~/.openharness/settings.json")

    async def _logout_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        settings = load_settings()
        profile_name = settings.resolve_profile()[0]
        AuthManager(settings).clear_profile_credential(profile_name)
        return CommandResult(message="Cleared stored API key.")

    async def _fast_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = (
            context.app_state.get().fast_mode
            if context.app_state is not None
            else settings.fast_mode
        )
        action = args.strip() or "show"
        if action == "show":
            return CommandResult(message=f"Fast mode: {'on' if current else 'off'}")
        enabled = {"on": True, "off": False, "toggle": not current}.get(action)
        if enabled is None:
            return CommandResult(message="Usage: /fast [show|on|off|toggle]")
        settings.fast_mode = enabled
        save_settings(settings)
        if context.app_state is not None:
            context.app_state.set(fast_mode=enabled)
        # Fast mode lives in the prompt tail; refresh it so the model sees the
        # new guidance without touching the stable cacheable prefix.
        _refresh_runtime_prompt(context)
        return CommandResult(message=f"Fast mode {'enabled' if enabled else 'disabled'}.")

    def _refresh_runtime_prompt(context: CommandContext) -> None:
        # Rebuild the stable prompt for changes that affect it (personality,
        # model profile, tool profile). For per-turn toggles the rebuilt bytes
        # are identical; the turn context is refreshed separately.
        settings = load_settings()
        available_tools = (
            {tool.name for tool in context.tool_registry.list_tools()}
            if context.tool_registry is not None
            else None
        )
        context.engine.set_system_prompt_result(
            build_stable_system_prompt_result(
                settings,
                cwd=context.cwd,
                extra_skill_dirs=context.extra_skill_dirs,
                extra_plugin_roots=context.extra_plugin_roots,
                available_tools=available_tools,
            )
        )
        context.engine.refresh_turn_context()

    def _drop_override(context: CommandContext, key: str) -> None:
        """Stop a startup CLI flag from shadowing a runtime change."""
        if context.settings_overrides is not None:
            context.settings_overrides.pop(key, None)

    def _apply_reasoning_effort(value: str, context: CommandContext) -> None:
        settings = load_settings()
        settings.effort = value
        save_settings(settings)
        _drop_override(context, "effort")
        context.engine.set_effort(value)
        _refresh_runtime_prompt(context)
        if context.app_state is not None:
            context.app_state.set(effort=value)

    async def _notify_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        from openharness.notifications import send_notification

        settings = load_settings()
        action = args.strip().lower() or "show"
        current = settings.notifications
        if action in {"show", "status"}:
            return CommandResult(
                message=(
                    f"Notifications: {'on' if current.enabled else 'off'}\n"
                    f"On turn complete: {'yes' if current.on_turn_complete else 'no'}\n"
                    f"Minimum duration: {current.min_duration_seconds:g}s\n"
                    "Use /notify on|off|test."
                )
            )
        if action in {"on", "enable", "true"}:
            settings.notifications.enabled = True
            save_settings(settings)
            return CommandResult(message="Desktop notifications enabled.")
        if action in {"off", "disable", "false"}:
            settings.notifications.enabled = False
            save_settings(settings)
            return CommandResult(message="Desktop notifications disabled.")
        if action == "test":
            sent = send_notification("Shim", "Test notification from Shim.")
            if sent:
                return CommandResult(message="Test notification sent.")
            return CommandResult(
                message=(
                    "Could not send a notification; the platform notifier or "
                    "PowerShell/notify-send is unavailable."
                )
            )
        return CommandResult(message="Usage: /notify [show|on|off|test]")

    async def _tools_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "show"
        profile = context.engine.tool_profile
        visible = context.engine.visible_tool_names
        all_tools = context.tool_registry.list_tools() if context.tool_registry else []

        if action in {"show", "status"}:
            hidden = (
                [tool.name for tool in all_tools if tool.name not in visible]
                if visible is not None
                else []
            )
            lines = [
                f"Tool profile: {profile}",
                f"Visible tools: {len(visible) if visible is not None else len(all_tools)}/{len(all_tools)}",
            ]
            if hidden:
                lines.append("Hidden until tool_search/enable: " + ", ".join(hidden))
            lines.append(
                "Use /tools profile core|full or /tools enable NAME."
            )
            return CommandResult(message="\n".join(lines))

        if action == "profile":
            if len(parts) < 2:
                return CommandResult(message="Usage: /tools profile core|full")
            target = parts[1].strip().lower()
            if target not in {"core", "full", "all"}:
                return CommandResult(message="Usage: /tools profile core|full")
            normalized = context.engine.set_tool_profile(target)
            settings.tool_profile = normalized
            save_settings(settings)
            _drop_override(context, "tool_profile")
            _refresh_runtime_prompt(context)
            if context.app_state is not None:
                context.app_state.set(tool_profile=normalized)
            return CommandResult(message=f"Tool profile set to {normalized}.")

        if action == "enable":
            if len(parts) < 2 or not parts[1].strip():
                return CommandResult(message="Usage: /tools enable NAME")
            name = parts[1].strip()
            if context.tool_registry is None or context.tool_registry.get(name) is None:
                return CommandResult(message=f"Unknown tool: {name}")
            context.engine.activate_tools([name])
            return CommandResult(message=f"Enabled tool for this session: {name}")

        return CommandResult(
            message="Usage: /tools [show|profile core|full|enable NAME]"
        )

    async def _personality_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = (
            context.app_state.get().personality
            if context.app_state is not None
            else settings.personality
        )
        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "show"

        if action in {"show", "status"}:
            persona = resolve_personality(current, settings.personality_custom)
            if persona is None:
                return CommandResult(
                    message=(
                        "Personality: off\n"
                        "Use /personality list to see presets, or "
                        "/personality set shim to enable one."
                    )
                )
            lines = [f"Personality: {persona.name}", persona.description]
            if persona.name == "custom":
                preview = settings.personality_custom.strip()
                if len(preview) > 240:
                    preview = preview[:240] + "..."
                lines.append(f"Custom text: {preview}")
            lines.append(
                "Use /personality list|set NAME|custom TEXT|off to change it."
            )
            return CommandResult(message="\n".join(lines))

        if action == "list":
            lines = ["Available personalities:"]
            for personality in list_personalities():
                lines.append(f"- {personality.name}: {personality.description}")
            lines.append("- off: disable the persona layer")
            lines.append("- custom TEXT: use your own persona text")
            return CommandResult(message="\n".join(lines))

        if action == "custom":
            text = parts[1].strip() if len(parts) > 1 else ""
            if not text:
                return CommandResult(
                    message="Usage: /personality custom TEXT"
                )
            settings.personality = "custom"
            settings.personality_custom = text
            save_settings(settings)
            _drop_override(context, "personality")
            _refresh_runtime_prompt(context)
            if context.app_state is not None:
                context.app_state.set(personality="custom")
            return CommandResult(
                message=f"Personality set to custom ({len(text)} chars)."
            )

        if action == "off":
            target = "off"
        elif action == "set":
            if len(parts) < 2:
                return CommandResult(
                    message="Usage: /personality set NAME"
                )
            target = parts[1].strip().lower()
            if target in {"default", "on"}:
                target = DEFAULT_PERSONALITY
            if target not in PERSONALITIES and target not in {"off", "none"}:
                return CommandResult(
                    message=(
                        f"Unknown personality: {target}. "
                        "Use /personality list."
                    )
                )
            if target in {"none"}:
                target = "off"
        else:
            return CommandResult(
                message=(
                    "Usage: /personality [show|list|set NAME|custom TEXT|off]"
                )
            )

        settings.personality = target
        save_settings(settings)
        _drop_override(context, "personality")
        _refresh_runtime_prompt(context)
        if context.app_state is not None:
            context.app_state.set(personality=target)
        if target == "off":
            return CommandResult(message="Personality disabled.")
        persona = resolve_personality(target, settings.personality_custom)
        name = persona.name if persona is not None else target
        return CommandResult(message=f"Personality set to {name}.")

    async def _effort_handler(args: str, context: CommandContext) -> CommandResult:
        current = (
            context.app_state.get().effort
            if context.app_state is not None
            else load_settings().effort
        )
        value = args.strip().lower() or "show"
        if value == "show":
            thinking = "off" if current == "off" else "on"
            return CommandResult(
                message=f"Reasoning effort: {current} (thinking {thinking})"
            )
        if value in {"off", "none", "disable", "disabled"}:
            value = "off"
        elif value == "max":
            value = "xhigh"
        if value not in {"low", "medium", "high", "xhigh", "off"}:
            return CommandResult(
                message="Usage: /effort [show|off|low|medium|high|xhigh|max]"
            )
        _apply_reasoning_effort(value, context)
        if value == "off":
            return CommandResult(
                message="Reasoning effort set to off. Extended thinking is disabled."
            )
        return CommandResult(message=f"Reasoning effort set to {value}.")

    def _thinking_preview(text: str, *, max_chars: int | None) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        if max_chars is None or len(stripped) <= max_chars:
            return stripped
        hidden = len(stripped) - max_chars
        return (
            f"{stripped[:max_chars]}\n"
            f"… ({hidden} more chars hidden; use /thinking show full)"
        )

    def _thinking_evidence(
        message: ConversationMessage | None,
        *,
        max_chars: int | None = 4000,
    ) -> str:
        if message is None:
            return "No assistant turn yet."
        thinking = [block for block in message.content if isinstance(block, ThinkingBlock)]
        reasoning = [block for block in message.content if isinstance(block, ReasoningBlock)]
        if thinking:
            chars = sum(len(block.thinking or "") for block in thinking)
            redacted = sum(1 for block in thinking if block.redacted)
            suffix = f", {redacted} redacted" if redacted else ""
            text = "\n\n".join(
                block.thinking.strip()
                for block in thinking
                if not block.redacted and (block.thinking or "").strip()
            )
            preview = _thinking_preview(text, max_chars=max_chars)
            evidence = (
                f"Last assistant turn: {len(thinking)} thinking block(s), "
                f"{chars} chars{suffix}"
            )
            if preview:
                evidence += f"\nLast thinking:\n{preview}"
            return evidence
        if reasoning:
            texts = [
                (block.text or "").strip()
                or "\n".join(part.strip() for part in block.summary if part.strip())
                for block in reasoning
            ]
            chars = sum(len(text) for text in texts)
            providers = ", ".join(sorted({block.provider for block in reasoning if block.provider}))
            suffix = f" from {providers}" if providers else ""
            text = "\n\n".join(texts)
            preview = _thinking_preview(text, max_chars=max_chars)
            evidence = (
                f"Last assistant turn: {len(reasoning)} reasoning block(s)"
                f"{suffix}, {chars} chars"
            )
            if preview:
                evidence += f"\nLast thinking:\n{preview}"
            return evidence
        return "Last assistant turn: no thinking/reasoning blocks."

    async def _probe_thinking(context: CommandContext, effort: str) -> CommandResult:
        request = ApiMessageRequest(
            model=context.engine.model,
            messages=[
                ConversationMessage.from_user_text(
                    "Compute 37*89. Reply with only the result."
                )
            ],
            max_tokens=4096,
            effort=effort,
        )
        final_message: ConversationMessage | None = None
        output_tokens = 0
        try:
            async for event in context.engine.api_client.stream_message(request):
                if isinstance(event, ApiMessageCompleteEvent):
                    final_message = event.message
                    output_tokens = event.usage.output_tokens
        except Exception as exc:  # noqa: BLE001 - probe failures are reported to the user
            return CommandResult(message=f"Thinking probe failed: {exc}")

        if final_message is None:
            return CommandResult(message="Thinking probe returned no final message.")

        blocks = [
            block
            for block in final_message.content
            if isinstance(block, ThinkingBlock | ReasoningBlock)
        ]
        chars = sum(
            len(getattr(block, "thinking", "") or getattr(block, "text", "") or "")
            for block in blocks
        )
        if blocks:
            verdict = (
                f"thinking is active ({len(blocks)} block(s), {chars} chars)"
            )
        elif effort == "off":
            verdict = "no thinking blocks returned; thinking is off or unsupported"
        else:
            verdict = (
                "no thinking blocks returned; the provider may ignore effort "
                "or may not expose reasoning"
            )
        return CommandResult(
            message=(
                f"Thinking probe: model={context.engine.model}, effort={effort}\n"
                f"- thinking/reasoning blocks: {len(blocks)} ({chars} chars)\n"
                f"- output tokens: {output_tokens}\n"
                f"- verdict: {verdict}"
            )
        )

    async def _thinking_handler(args: str, context: CommandContext) -> CommandResult:
        current = (
            context.app_state.get().effort
            if context.app_state is not None
            else load_settings().effort
        )
        parts = args.strip().lower().split()
        action = parts[0] if parts else "show"
        enabled = current != "off"
        if action == "show":
            show_full = len(parts) > 1 and parts[1] == "full"
            last_assistant = next(
                (
                    message
                    for message in reversed(context.engine.messages)
                    if message.role == "assistant"
                ),
                None,
            )
            return CommandResult(
                message=(
                    f"Thinking: {'on' if enabled else 'off'} (effort={current})\n"
                    f"Model: {context.engine.model}\n"
                    f"{_thinking_evidence(last_assistant, max_chars=None if show_full else 4000)}\n"
                    "Use /thinking probe to send a minimal live request."
                )
            )
        if action == "probe":
            override = parts[1] if len(parts) > 1 else ""
            if override in {"on", "enable", "true"}:
                probe_effort = "medium"
            elif override in {"off", "disable", "false"}:
                probe_effort = "off"
            elif override:
                return CommandResult(message="Usage: /thinking probe [on|off]")
            else:
                probe_effort = current
            return await _probe_thinking(context, probe_effort)
        if action in {"on", "enable", "true"}:
            target = current if enabled else "medium"
        elif action in {"off", "disable", "false"}:
            target = "off"
        elif action == "toggle":
            target = "off" if enabled else "medium"
        else:
            return CommandResult(
                message="Usage: /thinking [show [full]|on|off|toggle|probe [on|off]]"
            )
        _apply_reasoning_effort(target, context)
        return CommandResult(
            message=(
                f"Thinking {'enabled' if target != 'off' else 'disabled'}"
                f" (effort={target})."
            )
        )

    async def _passes_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = context.app_state.get().passes if context.app_state is not None else settings.passes
        value = args.strip() or "show"
        if value == "show":
            return CommandResult(message=f"Passes: {current}")
        try:
            passes = max(1, min(int(value), 8))
        except ValueError:
            return CommandResult(message="Usage: /passes [show|COUNT]")
        settings.passes = passes
        save_settings(settings)
        _refresh_runtime_prompt(context)
        if context.app_state is not None:
            context.app_state.set(passes=passes)
        return CommandResult(message=f"Pass count set to {passes}.")

    async def _turns_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        engine_turns = "unlimited" if context.engine.max_turns is None else str(context.engine.max_turns)
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            return CommandResult(
                message=(
                    f"Max turns (engine): {engine_turns}\n"
                    f"Max turns (config): {settings.max_turns}\n"
                    "Usage: /turns [show|unlimited|COUNT]"
                )
            )
        if tokens[0] == "set" and len(tokens) == 2:
            raw = tokens[1]
        elif len(tokens) == 1:
            raw = tokens[0]
        else:
            return CommandResult(message="Usage: /turns [show|unlimited|COUNT]")
        if raw.lower() == "unlimited":
            context.engine.set_max_turns(None)
            return CommandResult(
                message=(
                    "Max turns set to unlimited for this session. "
                    f"Saved config remains {settings.max_turns}."
                )
            )
        try:
            turns = int(raw)
        except ValueError:
            return CommandResult(message="Usage: /turns [show|unlimited|COUNT]")
        turns = max(1, min(turns, 512))
        settings.max_turns = turns
        save_settings(settings)
        _drop_override(context, "max_turns")
        context.engine.set_max_turns(turns)
        return CommandResult(message=f"Max turns set to {turns}.")

    async def _permissions_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            permission = settings.permission
            label = _MODE_LABELS.get(permission.mode.value, permission.mode.value)
            return CommandResult(
                message=(
                    f"Mode: {label}\n"
                    f"Allowed tools: {permission.allowed_tools}\n"
                    f"Denied tools: {permission.denied_tools}"
                )
            )
        target_mode: str | None = None
        if tokens[0] == "set" and len(tokens) == 2:
            target_mode = tokens[1]
        elif len(tokens) == 1 and tokens[0] in _MODE_LABELS:
            target_mode = tokens[0]
        if target_mode is not None:
            settings.permission.set_mode(PermissionMode(target_mode))
            save_settings(settings)
            _drop_override(context, "permission_mode")
            # Mutate the live checker instead of replacing it: the security
            # policy holds a reference to this exact object.
            context.engine.set_permission_mode(PermissionMode(target_mode))
            if context.app_state is not None:
                context.app_state.set(permission_mode=settings.permission.mode.value)
            label = _MODE_LABELS.get(target_mode, target_mode)
            return CommandResult(message=f"Permission mode set to {label}", refresh_runtime=True)
        return CommandResult(message="Usage: /permissions [show|default|full_auto|plan]")

    async def _plan_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        mode = args.strip() or "on"
        if mode in {"on", "enter"}:
            settings.permission.set_mode(PermissionMode.PLAN)
            save_settings(settings)
            _drop_override(context, "permission_mode")
            # Mutate the live checker instead of replacing it: the security
            # policy holds a reference to this exact object.
            context.engine.set_permission_mode(PermissionMode.PLAN)
            if context.app_state is not None:
                context.app_state.set(permission_mode=settings.permission.mode.value)
            return CommandResult(message="Plan mode enabled.", refresh_runtime=True)
        if mode in {"off", "exit"}:
            # Exiting plan mode resumes in auto, matching the model-facing
            # exit_plan_mode tool. Users who want prompt-everything can run
            # /permissions default.
            settings.permission.set_mode(PermissionMode.FULL_AUTO)
            save_settings(settings)
            _drop_override(context, "permission_mode")
            # Mutate the live checker instead of replacing it: the security
            # policy holds a reference to this exact object.
            context.engine.set_permission_mode(PermissionMode.FULL_AUTO)
            if context.app_state is not None:
                context.app_state.set(permission_mode=settings.permission.mode.value)
            return CommandResult(message="Plan mode disabled. Permission mode set to Auto.", refresh_runtime=True)
        return CommandResult(message="Usage: /plan [on|off]")

    async def _theme_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.themes import list_themes, load_theme

        settings = load_settings()
        tokens = args.split(maxsplit=1)
        current = (
            context.app_state.get().theme
            if context.app_state is not None and hasattr(context.app_state.get(), "theme")
            else settings.theme
        )

        if not tokens or tokens[0] == "show":
            try:
                theme = load_theme(current)
                lines = [
                    f"Theme: {theme.name}",
                    (f"  Colors:  primary={theme.colors.primary}  secondary={theme.colors.secondary}"
                    f"  accent={theme.colors.accent}  error={theme.colors.error}"
                    f"  muted={theme.colors.muted}"),
                    f"           background={theme.colors.background}  foreground={theme.colors.foreground}",
                    f"  Borders: style={theme.borders.style}",
                    (f"  Icons:   spinner={theme.icons.spinner}  tool={theme.icons.tool}"
                    f"  error={theme.icons.error}  success={theme.icons.success}"
                    f"  agent={theme.icons.agent}"),
                    (f"  Layout:  compact={theme.layout.compact}"
                    f"  show_tokens={theme.layout.show_tokens}"
                    f"  show_time={theme.layout.show_time}"),
                ]
                return CommandResult(message="\n".join(lines))
            except KeyError:
                return CommandResult(message=f"Theme: {current} (not found)")

        if tokens[0] == "list":
            available = list_themes()
            lines = [f"{'*' if name == current else ' '} {name}" for name in available]
            return CommandResult(message="\n".join(lines))

        if tokens[0] == "set" and len(tokens) == 2:
            name = tokens[1]
        elif len(tokens) == 1 and tokens[0] not in {"list", "preview"}:
            name = tokens[0]
        else:
            name = None
        if name is not None:
            try:
                load_theme(name)
            except KeyError:
                available = list_themes()
                return CommandResult(
                    message=f"Unknown theme: {name!r}. Available: {', '.join(available)}"
                )
            settings.theme = name
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(theme=name)
            return CommandResult(message=f"Theme set to {name}")

        if tokens[0] == "preview" and len(tokens) == 2:
            name = tokens[1]
            try:
                theme = load_theme(name)
            except KeyError:
                available = list_themes()
                return CommandResult(
                    message=f"Unknown theme: {name!r}. Available: {', '.join(available)}"
                )
            lines = [
                f"Preview: {theme.name}",
                f"  primary    {theme.colors.primary}",
                f"  secondary  {theme.colors.secondary}",
                f"  accent     {theme.colors.accent}",
                f"  error      {theme.colors.error}",
                f"  muted      {theme.colors.muted}",
                f"  background {theme.colors.background}",
                f"  foreground {theme.colors.foreground}",
                f"  borders    {theme.borders.style}",
                (f"  icons      spinner={theme.icons.spinner} tool={theme.icons.tool}"
                f" success={theme.icons.success} error={theme.icons.error}"
                f" agent={theme.icons.agent}"),
            ]
            return CommandResult(message="\n".join(lines))

        return CommandResult(message="Usage: /theme [list|show|NAME|preview NAME]")

    async def _output_style_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split(maxsplit=1)
        styles = load_output_styles()
        available = {style.name: style for style in styles}
        current = (
            context.app_state.get().output_style
            if context.app_state is not None
            else settings.output_style
        )
        if not tokens or tokens[0] == "show":
            return CommandResult(message=f"Output style: {current}")
        if tokens[0] == "list":
            return CommandResult(
                message="\n".join(f"{style.name} [{style.source}]" for style in styles)
            )
        if tokens[0] == "set" and len(tokens) == 2:
            style_name = tokens[1]
        elif len(tokens) == 1 and tokens[0] not in {"list"}:
            style_name = tokens[0]
        else:
            style_name = None
        if style_name is not None:
            if style_name not in available:
                return CommandResult(message=f"Unknown output style: {style_name}")
            settings.output_style = style_name
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(output_style=style_name)
            return CommandResult(message=f"Output style set to {style_name}")
        return CommandResult(message="Usage: /output-style [show|list|NAME]")

    async def _keybindings_handler(_: str, context: CommandContext) -> CommandResult:
        from openharness.keybindings import get_keybindings_path, load_keybindings

        bindings = (
            context.app_state.get().keybindings
            if context.app_state is not None and context.app_state.get().keybindings
            else load_keybindings()
        )
        lines = [f"Keybindings file: {get_keybindings_path()}"]
        lines.extend(f"{key} -> {command}" for key, command in sorted(bindings.items()))
        return CommandResult(message="\n".join(lines))

    async def _vim_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = (
            context.app_state.get().vim_enabled
            if context.app_state is not None
            else settings.vim_mode
        )
        action = args.strip() or "show"
        if action == "show":
            return CommandResult(message=f"Vim mode: {'on' if current else 'off'}")
        enabled = {"on": True, "off": False, "toggle": not current}.get(action)
        if enabled is None:
            return CommandResult(message="Usage: /vim [show|on|off|toggle]")
        settings.vim_mode = enabled
        save_settings(settings)
        if context.app_state is not None:
            context.app_state.set(vim_enabled=enabled)
        return CommandResult(message=f"Vim mode {'enabled' if enabled else 'disabled'}.")

    def _is_checklist_line(line: str) -> bool:
        return line.strip().startswith("- [")

    async def _clear_session_checklist(context: CommandContext) -> CommandResult:
        """Drop every checklist item from the session plan file.

        Only ``- [...]`` lines are removed: the plan file may also hold prose the
        model wrote, and ``/todos clear`` is about the items, not the document.
        The panel repaints because ``backend_host._refresh_plan_file_todo`` now
        compares the checklist content, not just the path.
        """
        engine = context.engine
        metadata = getattr(engine, "tool_metadata", None) or {}
        plan_file = str(metadata.get("plan_file") or "")
        if not plan_file:
            return CommandResult(message="No session checklist to clear.")
        path = Path(plan_file).expanduser()
        if not path.is_absolute():
            path = Path(context.cwd) / path
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            return CommandResult(message="No session checklist to clear.")

        removed = sum(1 for line in original.splitlines() if _is_checklist_line(line))
        if removed == 0:
            return CommandResult(message="The session checklist is already empty.")

        kept = [line for line in original.splitlines() if not _is_checklist_line(line)]
        text = "\n".join(kept).rstrip()
        text = f"{text}\n" if text.strip() else "# Plan\n"
        from openharness.utils.fs import write_text_preserving_newlines

        write_text_preserving_newlines(path, text)
        return CommandResult(
            message=f"Cleared {removed} checklist item{'s' if removed != 1 else ''}."
        )

    async def _todos_handler(args: str, context: CommandContext) -> CommandResult:
        """Show or change the todo/plan panel shape without touching the prompt."""
        settings = load_settings()
        modes = TODO_PANEL_MODES
        current = settings.todo_panel
        if context.app_state is not None:
            current = context.app_state.get().todo_panel
        if current not in modes:
            current = "full"

        tokens = args.split(maxsplit=1)
        if tokens and tokens[0] in {"show", "status"}:
            return CommandResult(message=f"Todo panel: {current} (ctrl+t cycles)")
        if tokens and tokens[0].lower() == "clear":
            return await _clear_session_checklist(context)
        target = tokens[0].lower() if tokens else modes[(modes.index(current) + 1) % len(modes)]
        if target not in modes:
            return CommandResult(message="Usage: /todos [full|compact|hidden|clear]")

        settings.todo_panel = target
        save_settings(settings)
        if context.app_state is not None:
            context.app_state.set(todo_panel=target)
        return CommandResult(message=f"Todo panel: {target} (ctrl+t cycles)")

    async def _voice_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.voice import extract_keyterms, inspect_voice_capabilities

        settings = load_settings()
        diagnostics = inspect_voice_capabilities(detect_provider(settings))
        current = (
            context.app_state.get().voice_enabled
            if context.app_state is not None
            else settings.voice_mode
        )
        tokens = args.split(maxsplit=1)
        if not tokens or tokens[0] == "show":
            return CommandResult(
                message=(
                    f"Voice mode: {'on' if current else 'off'}\n"
                    f"Available: {'yes' if diagnostics.available else 'no'}\n"
                    f"Recorder: {diagnostics.recorder or '(none)'}\n"
                    f"Reason: {diagnostics.reason}"
                )
            )
        if tokens[0] == "keyterms" and len(tokens) == 2:
            keyterms = extract_keyterms(tokens[1])
            return CommandResult(message="\n".join(keyterms) if keyterms else "(no keyterms)")
        enabled = {"on": True, "off": False, "toggle": not current}.get(tokens[0])
        if enabled is None:
            return CommandResult(message="Usage: /voice [show|on|off|toggle|keyterms TEXT]")
        settings.voice_mode = enabled
        save_settings(settings)
        if context.app_state is not None:
            context.app_state.set(
                voice_enabled=enabled,
                voice_available=diagnostics.available,
                voice_reason=diagnostics.reason,
            )
        return CommandResult(message=f"Voice mode {'enabled' if enabled else 'disabled'}.")

    async def _doctor_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        manager = AuthManager(settings)
        active_profile_name, active_profile = settings.resolve_profile()
        memory_dir = get_project_memory_dir(context.cwd)
        state = context.app_state.get() if context.app_state is not None else None
        lines = [
            "Doctor summary:",
            f"- cwd: {context.cwd}",
            f"- active_profile: {active_profile_name}",
            f"- model: {settings.model}",
            f"- provider_workflow: {active_profile.label}",
            f"- auth_source: {active_profile.auth_source}",
            f"- permission_mode: {state.permission_mode if state is not None else settings.permission.mode}",
            f"- theme: {state.theme if state is not None else settings.theme}",
            f"- output_style: {state.output_style if state is not None else settings.output_style}",
            f"- vim_mode: {'on' if (state.vim_enabled if state is not None else settings.vim_mode) else 'off'}",
            f"- voice_mode: {'on' if (state.voice_enabled if state is not None else settings.voice_mode) else 'off'}",
            f"- effort: {state.effort if state is not None else settings.effort}",
            f"- passes: {state.passes if state is not None else settings.passes}",
            f"- personality: {state.personality if state is not None else settings.personality}",
            f"- tool_profile: {state.tool_profile if state is not None else settings.tool_profile}",
            f"- memory_dir: {memory_dir}",
            f"- plugin_count: {max(len(context.plugin_summary.splitlines()) - 1, 0) if context.plugin_summary else 0}",
            f"- mcp_configured: {'yes' if context.mcp_summary and 'No MCP' not in context.mcp_summary else 'no'}",
            f"- auth_configured: {'yes' if manager.get_profile_statuses()[active_profile_name]['configured'] else 'no'}",
        ]
        return CommandResult(message="\n".join(lines))

    async def _privacy_settings_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        session_dir = context.session_backend.get_session_dir(context.cwd)
        lines = [
            "Privacy settings:",
            f"- user_config_dir: {get_config_dir()}",
            f"- project_config_dir: {get_project_config_dir(context.cwd)}",
            f"- session_dir: {session_dir}",
            f"- feedback_log: {get_feedback_log_path()}",
            f"- api_base_url: {settings.base_url or '(default Anthropic-compatible endpoint)'}",
            "- network: enabled only for provider and explicit web/MCP calls",
            "- storage: local files under ~/.openharness and project .openharness",
        ]
        return CommandResult(message="\n".join(lines))

    async def _rate_limit_options_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        provider = "moonshot-compatible" if (settings.base_url and "moonshot" in settings.base_url) else "anthropic-compatible"
        lines = [
            "Rate limit options:",
            f"- provider: {provider}",
            "- reduce /passes or switch /effort low for lighter requests",
            "- enable /fast for shorter responses and less tool churn",
            "- use /compact to shrink long transcripts before retrying",
            "- prefer background /tasks for long-running local work",
        ]
        return CommandResult(message="\n".join(lines))

    async def _release_notes_handler(_: str, context: CommandContext) -> CommandResult:
        path = Path(context.cwd) / "RELEASE_NOTES.md"
        if path.exists():
            return CommandResult(message=path.read_text(encoding="utf-8"))
        return CommandResult(
            message=(
                "# Release Notes\n\n"
                "- React TUI is now the default `oh` interface.\n"
                "- Added richer session, files, bridge, agent, copy, rewind, effort, passes, and privacy commands.\n"
                "- Expanded real-model validation across tools, MCP, tasks, plugins, notebook, LSP, cron, and worktree flows.\n"
            )
        )

    async def _upgrade_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(
            message=(
                f"Current version: {__version__}\n"
                "Upgrade instructions:\n"
                "- uv sync --extra dev\n"
                "- uv pip install -e .\n"
                "- npm --prefix frontend/terminal install"
            )
        )

    registry.register(
        SlashCommand(
            "login",
            "Show auth status or store an API key",
            _login_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "logout",
            "Clear the stored API key",
            _logout_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "config",
            "Show or update configuration",
            _config_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "permissions",
            "Show or update permission mode; Tab in the TUI opens the mode picker",
            _permissions_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "plan",
            "Toggle plan permission mode; /permissions default|full_auto exits plan explicitly",
            _plan_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(SlashCommand("fast", "Show or update fast mode", _fast_handler))

    registry.register(SlashCommand("effort", "Show or update reasoning effort", _effort_handler))
    registry.register(
        SlashCommand("thinking", "Show or toggle extended thinking", _thinking_handler)
    )
    registry.register(
        SlashCommand(
            "personality",
            "Show or switch the agent persona",
            _personality_handler,
        )
    )
    registry.register(
        SlashCommand(
            "tools",
            "Show or switch tool visibility and load deferred tools",
            _tools_handler,
        )
    )
    registry.register(
        SlashCommand(
            "notify",
            "Show or toggle desktop notifications for completed turns",
            _notify_handler,
        )
    )

    registry.register(SlashCommand("passes", "Show or update reasoning pass count", _passes_handler))

    registry.register(SlashCommand("turns", "Show or update maximum agentic turn count", _turns_handler))

    registry.register(SlashCommand("theme", "List, set, show or preview TUI themes", _theme_handler))

    registry.register(SlashCommand("output-style", "Show or update output style", _output_style_handler))

    registry.register(SlashCommand("keybindings", "Show resolved keybindings", _keybindings_handler, busy_safe=True))

    registry.register(
        SlashCommand(
            "todos",
            "Show, set or clear the todo panel (full|compact|hidden|clear)",
            _todos_handler,
            busy_safe=True,
        )
    )

    registry.register(SlashCommand("vim", "Show or update Vim mode", _vim_handler))

    registry.register(SlashCommand("voice", "Show or update voice mode", _voice_handler))

    registry.register(SlashCommand("doctor", "Show environment diagnostics", _doctor_handler, busy_safe=True))

    registry.register(SlashCommand("privacy-settings", "Show local privacy and storage settings", _privacy_settings_handler, busy_safe=True))

    registry.register(SlashCommand("rate-limit-options", "Show ways to reduce provider rate pressure", _rate_limit_options_handler, busy_safe=True))

    registry.register(SlashCommand("release-notes", "Show recent OpenHarness release notes", _release_notes_handler, busy_safe=True))

    registry.register(SlashCommand("upgrade", "Show upgrade instructions", _upgrade_handler, busy_safe=True))
