"""Provider-family prompt profiles (Claude / GPT / Codex)."""

from __future__ import annotations

from dataclasses import dataclass

from openharness.config.settings import Settings


@dataclass(frozen=True)
class PromptProfile:
    """Behavioral guidance for one model/provider family."""

    name: str
    guidance: str


_CLAUDE = PromptProfile(
    name="claude",
    guidance=(
        "Anthropic/Claude provider: preserve tool_use/tool_result pairing, "
        "batch independent tool calls, use extended thinking when enabled, and "
        "keep final answers concise. Treat prior thinking blocks as opaque state."
    ),
)

_GPT = PromptProfile(
    name="gpt",
    guidance=(
        "OpenAI-compatible provider: prefer parallel tool calls, keep progress "
        "updates terse, avoid restating tool output, and put independent calls "
        "in one response instead of serializing them."
    ),
)

_CODEX = PromptProfile(
    name="codex",
    guidance=(
        "Codex/Responses provider: batch independent tool calls, keep progress "
        "updates terse, and rely on the harness for reasoning effort and item "
        "state. Do not reconstruct provider-native reasoning state manually."
    ),
)

_GENERIC = PromptProfile(
    name="generic",
    guidance=(
        "Use tools deliberately: batch independent calls, keep progress updates "
        "terse, and preserve any provider-native state the harness returns."
    ),
)


def select_prompt_profile(settings: Settings) -> PromptProfile:
    provider = (settings.provider or "").lower()
    api_format = (settings.api_format or "").lower()
    if provider == "openai_codex":
        return _CODEX
    if api_format == "openai":
        return _GPT
    if api_format == "anthropic":
        return _CLAUDE
    return _GENERIC


def build_profile_section(settings: Settings) -> str:
    profile = select_prompt_profile(settings)
    return f"# Model Profile\n{profile.guidance}"
