"""Compaction port (ADR-0010 D2).

The engine owns when compaction runs; the implementation lives in
``openharness.services.compact`` and is injected at assembly time.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from openharness.domain.messages import ConversationMessage


@runtime_checkable
class CompactPort(Protocol):
    """Context-compaction policy used by the turn executor."""

    def new_state(self) -> Any:
        """Create the per-turn mutable compaction state."""

    async def compact_if_needed(
        self,
        messages: list[ConversationMessage],
        *,
        state: Any,
        api_client: Any,
        model: str,
        system_prompt: str,
        progress_callback: Any,
        force: bool,
        trigger: str,
        hook_executor: Any,
        carryover_metadata: dict[str, object] | None,
        context_window_tokens: int | None,
        auto_compact_threshold_tokens: int | None,
    ) -> tuple[list[ConversationMessage], bool]:
        """Return ``(messages, was_compacted)`` for this step."""
