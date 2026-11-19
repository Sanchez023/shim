"""The prompt bytes for one session: stable text, cacheable prefix, turn context.

Split out of :class:`openharness.engine.query_engine.QueryEngine`.  The three
system-prompt fields are deliberately kept together: ADR-0008 requires the text
and its contiguous cacheable prefix to move as one unit, because a text-only
update silently moves Anthropic's explicit cache breakpoint off the boundary.

Nothing here decides *when* a rebuild happens -- the engine still drives that.
This object only owns the bytes, so the cache digests in
``tests/test_engine/test_cache_payload_digest.py`` can pin them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from openharness.domain.messages import ConversationMessage, TextBlock

PromptBuilder = Callable[
    [str, dict[str, object]],
    "tuple[str, str] | tuple[str, str, tuple[tuple[str, str], ...]]",
]
TurnContextBuilder = Callable[[str, dict[str, object]], str]


class PromptState:
    """Hold the stable prompt, its cacheable prefix and the live turn context."""

    def __init__(
        self,
        *,
        text: str,
        cacheable_prefix: str = "",
        section_hashes: tuple[tuple[str, str], ...] = (),
        prompt_builder: PromptBuilder | None = None,
        turn_context_builder: TurnContextBuilder | None = None,
    ) -> None:
        self._text = text
        self._cacheable_prefix = cacheable_prefix
        self._section_hashes = tuple(section_hashes)
        self._prompt_builder = prompt_builder
        self._turn_context_builder = turn_context_builder
        self._turn_context = ""
        self._last_user_text = ""
        self._active_message: ConversationMessage | None = None

    @property
    def text(self) -> str:
        """Return the active system prompt."""
        return self._text

    @property
    def cacheable_prefix(self) -> str:
        """Return the contiguous cacheable prefix of the stable prompt."""
        return self._cacheable_prefix

    @property
    def section_hashes(self) -> tuple[tuple[str, str], ...]:
        """Return the per-section hashes used for cache attribution."""
        return self._section_hashes

    @property
    def turn_context(self) -> str:
        """Return the dynamic context attached to the latest user message."""
        return self._turn_context

    def set(
        self,
        text: str,
        cacheable_prefix: str | None = None,
        section_hashes: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        """Replace the prompt bytes.

        ``cacheable_prefix`` and ``section_hashes`` default to "unchanged" so a
        caller that only has new text cannot accidentally leave the breakpoint
        pointing at a stale boundary.
        """
        self._text = text
        if cacheable_prefix is not None:
            self._cacheable_prefix = cacheable_prefix
        if section_hashes is not None:
            self._section_hashes = tuple(section_hashes)

    def set_from_result(self, result: Any) -> None:
        """Update text, cacheable prefix and section hashes from a build result."""
        self.set(
            result.text,
            result.cacheable_prefix,
            tuple(getattr(result, "section_hashes", ())),
        )

    def prepare_turn(self, user_text: str, metadata: dict[str, object]) -> None:
        """Rebuild the prompt (when it may have changed) and the turn context.

        ``metadata`` is read, never written: the tool-metadata dict is shared
        with the dispatcher and must stay the engine's to mutate.
        """
        if self._prompt_builder is not None:
            from openharness.prompts.active_files import (
                metadata_has_active_files,
                prompt_mentions_files,
            )

            if prompt_mentions_files(user_text) or metadata_has_active_files(metadata):
                with contextlib.suppress(Exception):
                    built = self._prompt_builder(user_text, metadata)
                    if len(built) == 3:
                        rebuilt_prompt, rebuilt_prefix, rebuilt_hashes = built
                    else:
                        rebuilt_prompt, rebuilt_prefix = built
                        rebuilt_hashes = ()
                    self.set(rebuilt_prompt, rebuilt_prefix, rebuilt_hashes)
        self._last_user_text = user_text
        if self._turn_context_builder is not None:
            with contextlib.suppress(Exception):
                self._turn_context = self._turn_context_builder(user_text, metadata)
        else:
            self._turn_context = ""

    def refresh_turn_context(self, metadata: dict[str, object]) -> None:
        """Rebuild the turn context after a runtime setting change."""
        if self._turn_context_builder is None:
            return
        with contextlib.suppress(Exception):
            self._turn_context = self._turn_context_builder(self._last_user_text, metadata)
        self._rewrite_active_message()

    def bind_turn_context_message(self, message: ConversationMessage | None) -> None:
        """Track the history message that carries the current turn context."""
        self._active_message = message

    def _rewrite_active_message(self) -> None:
        if self._active_message is not None:
            self._active_message.content = [
                TextBlock(
                    text=f"<turn_context>\n{self._turn_context}\n</turn_context>",
                    visibility="session_private",
                )
            ]
