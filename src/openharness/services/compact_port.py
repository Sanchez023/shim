"""Service-backed implementation of the engine compaction port."""

from __future__ import annotations

from typing import Any

from openharness.domain.messages import ConversationMessage
from openharness.services.compact import AutoCompactState


class ServiceCompactPort:
    """Delegate compaction to the existing service pipeline."""

    def new_state(self) -> AutoCompactState:
        return AutoCompactState()

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
        # Imported lazily so tests can monkeypatch the service function.
        from openharness.services.compact import auto_compact_if_needed

        return await auto_compact_if_needed(
            messages,
            api_client=api_client,
            model=model,
            system_prompt=system_prompt,
            state=state,
            progress_callback=progress_callback,
            force=force,
            trigger=trigger,
            hook_executor=hook_executor,
            carryover_metadata=carryover_metadata,
            context_window_tokens=context_window_tokens,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        )
