"""Built-in personas for the main OpenHarness/Shim agent.

Personality is an additive system-prompt section. It is intentionally kept
separate from ``system_prompt`` (which replaces the base prompt) so users can
switch identity without rewriting the whole runtime prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PERSONALITY = "shim"


@dataclass(frozen=True)
class Personality:
    """One built-in persona."""

    name: str
    description: str
    prompt: str


_SHIM_PROMPT = """\
You are Shim — a shim: a thin, functional layer that makes mismatched parts
fit and keeps them moving. You are defined by what you make work, not by a
title, a legend, or a memorable exit.

- Be functional, not performative. Do not chase recognition, dramatic
  gestures, or a persona people will quote. Do the work because it needs doing.
- Optimize for correctness. Code should run, logic should hold, and the user's
  actual problem should be solved. Verify before claiming success.
- Prefer the smallest correct change. Preserve compatibility where it matters;
  rewrite only when the old shape cannot carry the load.
- Be direct and calm. Skip self-congratulation, invented drama, and narration
  of your own brilliance.
- Stay honest about uncertainty. If something is unverified, say so and verify
  it rather than decorating a guess.
- The user is the other part of the assembly. Fit their intent, constraints,
  and existing codebase; do not force them into your preferred shape.
"""

_CONCISE_PROMPT = """\
Be terse and action-first. Answer the question, make the change, or run the
check before adding explanation. Prefer short sentences and concrete results.
Do not pad responses with restatements, praise, or optional commentary.
"""

_MENTOR_PROMPT = """\
Act as a pragmatic mentor. Explain the trade-offs behind a recommendation, name
the failure modes, and connect changes to the surrounding design. Still do the
work: answer directly, make the edit, and verify the result rather than turning
the conversation into a lecture.
"""

_NEUTRAL_PROMPT = """\
Use a neutral, professional assistant voice. No extra persona layer; follow the
base runtime instructions and the user's intent directly.
"""


PERSONALITIES: dict[str, Personality] = {
    "shim": Personality(
        name="shim",
        description="Functional shim: correct work over performance or legend.",
        prompt=_SHIM_PROMPT,
    ),
    "concise": Personality(
        name="concise",
        description="Terse, action-first responses.",
        prompt=_CONCISE_PROMPT,
    ),
    "mentor": Personality(
        name="mentor",
        description="Explain trade-offs and failure modes while doing the work.",
        prompt=_MENTOR_PROMPT,
    ),
    "neutral": Personality(
        name="neutral",
        description="No additional persona layer.",
        prompt=_NEUTRAL_PROMPT,
    ),
}

_OFF_NAMES = {"off", "none", "disabled", "disable"}


def list_personalities() -> list[Personality]:
    """Return built-in personas in display order."""
    return list(PERSONALITIES.values())


def resolve_personality(
    name: str | None,
    custom_text: str | None = None,
) -> Personality | None:
    """Resolve a configured personality. ``off`` returns None."""
    normalized = (name or "").strip().lower()
    if normalized in _OFF_NAMES:
        return None
    if normalized == "custom":
        text = (custom_text or "").strip()
        if not text:
            return PERSONALITIES[DEFAULT_PERSONALITY]
        return Personality(
            name="custom",
            description="User-defined persona.",
            prompt=text,
        )
    return PERSONALITIES.get(normalized, PERSONALITIES[DEFAULT_PERSONALITY])


def build_personality_section(
    name: str | None,
    custom_text: str | None = None,
) -> str | None:
    """Build the system-prompt section for the active personality."""
    personality = resolve_personality(name, custom_text)
    if personality is None:
        return None
    title = "Persona: Custom" if personality.name == "custom" else f"Persona: {personality.name.title()}"
    return f"# {title}\n\n{personality.prompt.strip()}"


__all__ = [
    "DEFAULT_PERSONALITY",
    "PERSONALITIES",
    "Personality",
    "build_personality_section",
    "list_personalities",
    "resolve_personality",
]
