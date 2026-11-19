"""Tests for the backend select-picker payload builders."""

from __future__ import annotations

from dataclasses import replace

from openharness.config.settings import Settings
from openharness.state import AppState
from openharness.ui.select_commands import (
    build_select_command_line,
    build_select_prompt,
    model_select_options,
)


def _state(**overrides: object) -> AppState:
    base = AppState(model="claude-test", permission_mode="default", theme="default")
    return replace(base, **overrides) if overrides else base


def test_build_select_command_line():
    assert build_select_command_line("resume", "") == "/resume"
    assert build_select_command_line("resume", "abc123") == "/resume abc123"
    assert build_select_command_line("model", "opus") == "/model opus"
    assert build_select_command_line("permissions", "plan") == "/permissions plan"
    assert build_select_command_line("unknown", "x") is None


def test_build_select_prompt_returns_none_for_unknown_command():
    assert build_select_prompt("unknown", settings=Settings(), state=_state(), max_turns=None) is None


def test_permissions_prompt_marks_the_live_mode():
    settings = Settings()
    prompt = build_select_prompt("permissions", settings=settings, state=_state(), max_turns=None)

    assert prompt is not None
    assert prompt.title == "Permission Mode"
    active = [option["value"] for option in prompt.options if option["active"]]
    assert active == [settings.permission.mode.value]


def test_passes_prefers_live_state_over_settings():
    settings = Settings().model_copy(update={"passes": 2})
    prompt = build_select_prompt(
        "passes", settings=settings, state=_state(passes=4), max_turns=None
    )

    assert prompt is not None
    assert [option["value"] for option in prompt.options] == [str(value) for value in range(1, 9)]
    assert [option["value"] for option in prompt.options if option["active"]] == ["4"]
    assert prompt.options[0]["label"] == "1 pass"
    assert prompt.options[1]["label"] == "2 passes"


def test_turns_includes_unlimited_and_the_live_limit():
    prompt = build_select_prompt("turns", settings=Settings(), state=_state(), max_turns=96)
    assert prompt is not None
    values = [option["value"] for option in prompt.options]
    assert values[0] == "unlimited"
    assert "96" in values
    assert {"32", "64", "128", "200", "256", "512"} <= set(values)
    assert [option["value"] for option in prompt.options if option["active"]] == ["96"]

    unlimited = build_select_prompt("turns", settings=Settings(), state=_state(), max_turns=None)
    assert unlimited is not None
    assert [option["value"] for option in unlimited.options if option["active"]] == ["unlimited"]


def test_toggle_prompts_reflect_state():
    fast = build_select_prompt("fast", settings=Settings(), state=_state(fast_mode=True), max_turns=None)
    vim = build_select_prompt("vim", settings=Settings(), state=_state(vim_enabled=True), max_turns=None)
    voice = build_select_prompt(
        "voice",
        settings=Settings(),
        state=_state(voice_enabled=True, voice_reason="no microphone"),
        max_turns=None,
    )

    assert fast is not None and vim is not None and voice is not None
    assert [option["value"] for option in fast.options if option["active"]] == ["on"]
    assert [option["value"] for option in vim.options if option["active"]] == ["on"]
    assert voice.options[0]["description"] == "no microphone"
    assert [option["value"] for option in voice.options if option["active"]] == ["on"]


def test_model_select_options_prefers_profile_allowlist():
    options = model_select_options("qwen", "openai", ["deepseek-chat", "qwen"])

    assert [option["value"] for option in options] == ["deepseek-chat", "qwen"]
    assert [option["value"] for option in options if option["active"]] == ["qwen"]
    assert {option["description"] for option in options} == {"Allowed for this profile"}


def test_model_select_options_keeps_current_model_first():
    options = model_select_options("qwen3-max", "dashscope")

    assert options[0]["value"] == "qwen3-max"
    assert options[0]["description"] == "Current model"
    assert len([option for option in options if option["value"] == "qwen3-max"]) == 1
