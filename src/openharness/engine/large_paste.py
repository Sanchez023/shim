"""Offload very large user pastes before they enter the prompt.

A single pasted document or log can be tens of thousands of tokens.  It is
new content, so the request that first carries it pays a full cache miss for
every token.  Saving the text to an artifact and sending a short preview plus
the path keeps the prompt small; the model can still read the exact content
with ``read_file`` (offset/limit) or ``grep``.

The transform applies only to pure-text user messages.  Images, tool results
and assistant messages are left untouched.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import uuid4

from openharness.config.paths import get_data_dir
from openharness.domain.messages import ConversationMessage, TextBlock

log = logging.getLogger(__name__)

DEFAULT_LARGE_PASTE_THRESHOLD_CHARS = 24_000
DEFAULT_LARGE_PASTE_PREVIEW_CHARS = 2_000


def user_paste_dir() -> Path:
    """Return the artifact directory for offloaded user pastes."""
    directory = get_data_dir() / "user_pastes"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_paste(text: str) -> Path:
    path = user_paste_dir() / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-paste-{uuid4().hex[:12]}.txt"
    )
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def _preview_block(text: str, preview_chars: int) -> str:
    if len(text) <= preview_chars * 2:
        return text
    return (
        f"--- first {preview_chars} chars ---\n"
        f"{text[:preview_chars]}\n"
        f"--- last {preview_chars} chars ---\n"
        f"{text[-preview_chars:]}"
    )


def _marker_text(text: str, path: Path, preview_chars: int) -> str:
    return (
        "[Large paste offloaded]\n"
        f"Original message size: {len(text)} chars\n"
        f"Full text saved to: {path}\n"
        "The full text was not sent to the model. Read it on demand with "
        "read_file (use offset/limit for long files) or grep on that path.\n"
        "\n"
        f"{_preview_block(text, max(0, preview_chars))}"
    )


def offload_large_paste_message(
    message: ConversationMessage,
    *,
    threshold_chars: int = DEFAULT_LARGE_PASTE_THRESHOLD_CHARS,
    preview_chars: int = DEFAULT_LARGE_PASTE_PREVIEW_CHARS,
) -> tuple[ConversationMessage, Path | None]:
    """Replace an oversized pure-text user message with a preview.

    Returns the original message unchanged when it is small enough or when it
    carries non-text content.  The full text is always preserved on disk.
    """
    if message.role != "user" or not message.content:
        return message, None
    if not all(isinstance(block, TextBlock) for block in message.content):
        return message, None
    text = message.text
    if len(text) < max(0, threshold_chars):
        return message, None

    try:
        path = _write_paste(text)
    except OSError as exc:
        # Never fail a user turn because the data directory is unwritable.
        log.warning("Could not offload large user paste: %s", exc)
        return message, None

    offloaded = ConversationMessage(
        role="user",
        content=[TextBlock(text=_marker_text(text, path, preview_chars))],
    )
    return offloaded, path
