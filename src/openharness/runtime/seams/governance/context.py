"""Context-governance seam.

The default governor defers to the existing compaction pipeline.  Alternative
governors can disable compaction or supply a replacement message plan without
editing ``run_query``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from openharness.domain.messages import ConversationMessage
from openharness.domain.usage import UsageSnapshot


@dataclass(frozen=True)
class ContextPlan:
    """A governor's decision for the next model step."""

    messages: list[ConversationMessage] | None = None
    skip_compaction: bool = False
    reason: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class ContextGovernor(Protocol):
    """Decides when and how conversation context is compacted."""

    async def on_step_pre(
        self,
        messages: list[ConversationMessage],
        *,
        model: str,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
    ) -> ContextPlan | None:
        """Return a plan, or None to use the existing pipeline."""

    async def on_request_error(
        self,
        messages: list[ConversationMessage],
        error: Exception,
    ) -> ContextPlan | None:
        """Handle an overflow-style request error."""

    async def after_turn(
        self,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
    ) -> None:
        """Run post-turn bookkeeping."""


class ExistingCompactGovernor:
    """Preserve the built-in auto/reactive compaction behaviour."""

    async def on_step_pre(
        self,
        messages: list[ConversationMessage],
        *,
        model: str,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
    ) -> ContextPlan | None:
        del messages, model, context_window_tokens, auto_compact_threshold_tokens
        return None

    async def on_request_error(
        self,
        messages: list[ConversationMessage],
        error: Exception,
    ) -> ContextPlan | None:
        del messages, error
        return None

    async def after_turn(
        self,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
    ) -> None:
        del messages, usage


class NoCompactGovernor:
    """Disable auto-compaction for tests or constrained deployments."""

    async def on_step_pre(
        self,
        messages: list[ConversationMessage],
        *,
        model: str,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
    ) -> ContextPlan | None:
        del messages, model, context_window_tokens, auto_compact_threshold_tokens
        return ContextPlan(skip_compaction=True, reason="compaction disabled by governor")

    async def on_request_error(
        self,
        messages: list[ConversationMessage],
        error: Exception,
    ) -> ContextPlan | None:
        del messages, error
        return ContextPlan(skip_compaction=True, reason="compaction disabled by governor")

    async def after_turn(
        self,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
    ) -> None:
        del messages, usage


def build_default_context_governor() -> ExistingCompactGovernor:
    return ExistingCompactGovernor()
