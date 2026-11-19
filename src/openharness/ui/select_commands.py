"""Select-picker payloads for the React backend host.

The backend host turns slash commands such as ``/model`` and ``/theme`` into
``select_request`` events. The option lists are pure data derived from settings
and app state, so they live here instead of inside the host class: the host
stays focused on the protocol and these builders are testable without a bundle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from openharness.auth.manager import AuthManager
from openharness.config.settings import CLAUDE_MODEL_ALIAS_OPTIONS, Settings, resolve_model_setting
from openharness.output_styles import load_output_styles
from openharness.state import AppState
from openharness.themes import list_themes

SelectOption = dict[str, object]
OptionBuilder = Callable[..., list[SelectOption]]


@dataclass(frozen=True)
class SelectPrompt:
    """One ``select_request`` payload: a title, the source command, and options."""

    title: str
    command: str
    options: list[SelectOption]


_SELECT_COMMANDS = frozenset(
    {
        "provider",
        "permissions",
        "theme",
        "output-style",
        "effort",
        "passes",
        "turns",
        "fast",
        "vim",
        "voice",
        "model",
        "resume",
    }
)


def build_select_command_line(command: str, value: str) -> str | None:
    """Return the slash command a picker selection should submit."""
    if command == "resume":
        return f"/resume {value}" if value else "/resume"
    if command in _SELECT_COMMANDS:
        return f"/{command} {value}"
    return None


def model_select_options(
    current_model: str,
    provider: str,
    allowed_models: list[str] | None = None,
) -> list[SelectOption]:
    """Return model choices for a provider profile."""
    if allowed_models:
        return [
            {
                "value": value,
                "label": value,
                "description": "Allowed for this profile",
                "active": value == current_model,
            }
            for value in allowed_models
        ]

    provider_name = provider.lower()
    if provider_name in {"anthropic", "anthropic_claude"}:
        resolved_current = resolve_model_setting(current_model, provider_name)
        return [
            {
                "value": value,
                "label": label,
                "description": description,
                "active": value == current_model
                or resolve_model_setting(value, provider_name) == resolved_current,
            }
            for value, label, description in CLAUDE_MODEL_ALIAS_OPTIONS
        ]

    families: list[tuple[str, str]] = []
    if provider_name in {"openai-codex", "openai", "openai-compatible", "openrouter", "github_copilot"}:
        families.extend(
            [
                ("gpt-5.4", "OpenAI flagship"),
                ("gpt-5", "General GPT-5"),
                ("gpt-4.1", "Stable GPT-4.1"),
                ("o4-mini", "Fast reasoning"),
            ]
        )
    elif provider_name in {"moonshot", "moonshot-compatible"}:
        families.extend(
            [
                ("kimi-k2.5", "Moonshot K2.5"),
                ("kimi-k2-turbo-preview", "Faster Moonshot"),
            ]
        )
    elif provider_name == "dashscope":
        families.extend(
            [
                ("qwen3.5-flash", "Fast Qwen"),
                ("qwen3-max", "Strong Qwen"),
                ("deepseek-r1", "Reasoning model"),
            ]
        )
    elif provider_name == "gemini":
        families.extend(
            [
                ("gemini-2.5-pro", "Gemini Pro"),
                ("gemini-2.5-flash", "Gemini Flash"),
            ]
        )
    elif provider_name == "minimax":
        families.extend(
            [
                ("MiniMax-M2.7", "MiniMax flagship"),
                ("MiniMax-M2.7-highspeed", "MiniMax fast"),
            ]
        )

    seen: set[str] = set()
    options: list[SelectOption] = []
    for value, description in [(current_model, "Current model"), *families]:
        if not value or value in seen:
            continue
        seen.add(value)
        options.append(
            {
                "value": value,
                "label": value,
                "description": description,
                "active": value == current_model,
            }
        )
    return options


def _provider_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del state, max_turns
    statuses = AuthManager(settings).get_profile_statuses()
    return [
        {
            "value": name,
            "label": info["label"],
            "description": (
                f"{info['provider']} / {info['auth_source']}"
                + (" [missing auth]" if not info["configured"] else "")
            ),
            "active": info["active"],
        }
        for name, info in statuses.items()
    ]


def _permission_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del state, max_turns
    mode = settings.permission.mode.value
    return [
        {
            "value": "default",
            "label": "Default",
            "description": "Ask before write/execute operations",
            "active": mode == "default",
        },
        {
            "value": "full_auto",
            "label": "Auto",
            "description": "Allow all tools automatically",
            "active": mode == "full_auto",
        },
        {
            "value": "plan",
            "label": "Plan Mode",
            "description": "Block all write operations",
            "active": mode == "plan",
        },
    ]


def _theme_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del state, max_turns
    return [
        {"value": name, "label": name, "active": name == settings.theme}
        for name in list_themes()
    ]


def _output_style_options(
    *, settings: Settings, state: AppState, max_turns: int | None
) -> list[SelectOption]:
    del state, max_turns
    return [
        {
            "value": style.name,
            "label": style.name,
            "description": style.source,
            "active": style.name == settings.output_style,
        }
        for style in load_output_styles()
    ]


def _effort_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del state, max_turns
    return [
        {"value": "low", "label": "Low", "description": "Fastest responses", "active": settings.effort == "low"},
        {"value": "medium", "label": "Medium", "description": "Balanced reasoning", "active": settings.effort == "medium"},
        {"value": "high", "label": "High", "description": "Deepest reasoning", "active": settings.effort == "high"},
        {"value": "xhigh", "label": "XHigh", "description": "Extra high reasoning", "active": settings.effort == "xhigh"},
    ]


def _passes_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del max_turns
    current = int(state.passes or settings.passes)
    return [
        {
            "value": str(value),
            "label": f"{value} pass{'es' if value != 1 else ''}",
            "active": value == current,
        }
        for value in range(1, 9)
    ]


def _turns_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del settings, state
    values = {32, 64, 128, 200, 256, 512}
    if isinstance(max_turns, int):
        values.add(max_turns)
    options: list[SelectOption] = [
        {
            "value": "unlimited",
            "label": "Unlimited",
            "description": "Do not hard-stop this session",
            "active": max_turns is None,
        }
    ]
    options.extend(
        {"value": str(value), "label": f"{value} turns", "active": value == max_turns}
        for value in sorted(values)
    )
    return options


def _fast_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del settings, max_turns
    current = bool(state.fast_mode)
    return [
        {"value": "on", "label": "On", "description": "Prefer shorter, faster responses", "active": current},
        {"value": "off", "label": "Off", "description": "Use normal response mode", "active": not current},
    ]


def _vim_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del settings, max_turns
    current = bool(state.vim_enabled)
    return [
        {"value": "on", "label": "On", "description": "Enable Vim keybindings", "active": current},
        {"value": "off", "label": "Off", "description": "Use standard keybindings", "active": not current},
    ]


def _voice_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del settings, max_turns
    current = bool(state.voice_enabled)
    return [
        {
            "value": "on",
            "label": "On",
            "description": state.voice_reason or "Enable voice mode",
            "active": current,
        },
        {"value": "off", "label": "Off", "description": "Disable voice mode", "active": not current},
    ]


def _model_options(*, settings: Settings, state: AppState, max_turns: int | None) -> list[SelectOption]:
    del state, max_turns
    _, active_profile = settings.resolve_profile()
    return model_select_options(settings.model, active_profile.provider, active_profile.allowed_models)


_SELECT_TITLES = {
    "provider": "Provider Profile",
    "permissions": "Permission Mode",
    "theme": "Theme",
    "output-style": "Output Style",
    "effort": "Reasoning Effort",
    "passes": "Reasoning Passes",
    "turns": "Max Turns",
    "fast": "Fast Mode",
    "vim": "Vim Mode",
    "voice": "Voice Mode",
    "model": "Model",
}

_OPTION_BUILDERS: dict[str, OptionBuilder] = {
    "provider": _provider_options,
    "permissions": _permission_options,
    "theme": _theme_options,
    "output-style": _output_style_options,
    "effort": _effort_options,
    "passes": _passes_options,
    "turns": _turns_options,
    "fast": _fast_options,
    "vim": _vim_options,
    "voice": _voice_options,
    "model": _model_options,
}


def build_select_prompt(
    command: str,
    *,
    settings: Settings,
    state: AppState,
    max_turns: int | None,
) -> SelectPrompt | None:
    """Return the picker payload for a slash command, or None if it has no picker."""
    normalized = command.strip().lstrip("/").lower()
    title = _SELECT_TITLES.get(normalized)
    builder = _OPTION_BUILDERS.get(normalized)
    if title is None or builder is None:
        return None
    return SelectPrompt(
        title=title,
        command=normalized,
        options=builder(settings=settings, state=state, max_turns=max_turns),
    )
