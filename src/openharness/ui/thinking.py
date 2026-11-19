"""Helpers for surfacing model reasoning in UI surfaces.

Thinking blocks carry ``session_private`` visibility so they never leak into
assistant text or the durable projections. When the user asks to see the
reasoning process, these helpers extract the human-readable parts explicitly.
"""

from __future__ import annotations

from openharness.domain.messages import ConversationMessage, ReasoningBlock, ThinkingBlock


def extract_thinking_text(message: ConversationMessage | None) -> str:
    """Return the user-readable thinking/reasoning text in a message.

    Redacted thinking blocks carry only an opaque payload, so they are skipped.
    Provider-native reasoning blocks are rendered from their summary text.
    """
    if message is None:
        return ""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, ThinkingBlock):
            if block.redacted or not block.thinking.strip():
                continue
            parts.append(block.thinking.strip())
        elif isinstance(block, ReasoningBlock):
            text = (block.text or "").strip()
            if not text:
                text = "\n".join(part.strip() for part in block.summary if part.strip())
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def truncate_thinking(text: str, *, max_chars: int) -> tuple[str, int]:
    """Return ``(shown_text, hidden_chars)`` for a thinking preview."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, 0
    return text[:max_chars], len(text) - max_chars
