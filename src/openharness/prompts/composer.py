"""Composable system-prompt sections and prompt budgeting."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_PROMPT_CHARS = 60_000
PROMPT_VERSION = "2026-11-17.1"


@dataclass(frozen=True)
class PromptSection:
    """One independently managed prompt section."""

    name: str
    content: str
    order: int = 100
    cacheable: bool = False
    required: bool = False
    max_chars: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptBuildResult:
    """Composed prompt text plus section diagnostics."""

    text: str
    sections: list[dict[str, Any]]
    cacheable_prefix: str = ""
    version: str = PROMPT_VERSION
    #: Ordered ``(section_name, sha1)`` pairs for prefix-drift attribution.
    section_hashes: tuple[tuple[str, str], ...] = ()


class PromptComposer:
    """Order, trim, budget, and join prompt sections deterministically."""

    def __init__(self, *, max_total_chars: int = DEFAULT_MAX_PROMPT_CHARS) -> None:
        self.max_total_chars = max(1_000, int(max_total_chars))

    def compose(self, sections: Iterable[PromptSection]) -> PromptBuildResult:
        ordered = sorted(
            (section for section in sections if section.content.strip()),
            key=lambda section: (section.order, section.name),
        )
        used = 0
        parts: list[str] = []
        prefix_parts: list[str] = []
        in_cacheable_prefix = True
        diagnostics: list[dict[str, Any]] = []

        for section in ordered:
            content = section.content.strip()
            truncated = False
            if section.max_chars is not None and len(content) > section.max_chars:
                content = (
                    content[: max(0, section.max_chars - 20)].rstrip()
                    + "\n...[truncated]..."
                )
                truncated = True

            cost = len(content) + 2
            remaining = self.max_total_chars - used
            if not section.required and cost > remaining:
                # Try a partial inclusion for large optional sections.
                if remaining > 500:
                    content = (
                        content[: remaining - 40].rstrip()
                        + "\n...[truncated by prompt budget]..."
                    )
                    cost = len(content) + 2
                    truncated = True
                else:
                    diagnostics.append(
                        {
                            "name": section.name,
                            "order": section.order,
                            "chars": 0,
                            "cacheable": section.cacheable,
                            "included": False,
                            "truncated": False,
                            "sha1": "",
                        }
                    )
                    continue

            parts.append(content)
            if in_cacheable_prefix and section.cacheable:
                prefix_parts.append(content)
            else:
                in_cacheable_prefix = False
            used += cost
            digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
            diagnostics.append(
                {
                    "name": section.name,
                    "order": section.order,
                    "chars": len(content),
                    "cacheable": section.cacheable,
                    "included": True,
                    "truncated": truncated,
                    "sha1": digest,
                }
            )

        return PromptBuildResult(
            text="\n\n".join(parts),
            sections=diagnostics,
            cacheable_prefix="\n\n".join(prefix_parts),
            section_hashes=tuple(
                (item["name"], item["sha1"])
                for item in diagnostics
                if item.get("included")
            ),
        )
