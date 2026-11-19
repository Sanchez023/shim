"""Helpers shared by the turn and step executors.

These used to live in ``engine.query`` and were imported lazily from
``turn_executor``/``step_runner``, which hid a module cycle. Keeping them in a
neutral module lets the executors import them directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from openharness.api.errors import (
    completion_token_limit,
    is_completion_token_limit_error,
    is_context_length_error,
)
from openharness.api.provider import is_model_multimodal
from openharness.domain.messages import ConversationMessage, ImageBlock, TextBlock
from openharness.domain.stream_events import StatusEvent, StreamEvent
from openharness.tools.base import ToolExecutionContext

MAX_SAFE_COMPLETION_TOKENS = 128_000

_IMAGE_PREPROCESS_STATUS = "Converting image to text description via vision model…"

# Error classification lives in ``api.errors``; these aliases preserve the
# historical import sites inside the engine.
_is_prompt_too_long_error = is_context_length_error
_is_completion_token_limit_error = is_completion_token_limit_error
_extract_completion_token_limit = completion_token_limit


def _bounded_completion_tokens(max_tokens: int, context_window_tokens: int | None) -> int:
    """Clamp requested completion tokens to a value the provider will accept.

    Oversized user config should not make every turn fail.  The cap keeps the
    request below the documented maximum while preserving normal defaults.
    """
    limit = MAX_SAFE_COMPLETION_TOKENS
    if context_window_tokens is not None and context_window_tokens > 0:
        limit = min(limit, int(context_window_tokens))
    return max(1, min(int(max_tokens), limit))


def _serialize_messages(messages: list[ConversationMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(mode="json") for message in messages]


async def _preprocess_images_in_messages(
    messages: list[ConversationMessage],
    context: Any,
) -> AsyncIterator[StreamEvent]:
    """Scan messages for ImageBlocks and convert them to text if the active
    model does not support multimodal input.

    Yields status events during conversion so the UI stays responsive.
    """
    if is_model_multimodal(context.model):
        return
    if context.tool_metadata is None:
        return

    vision_config = context.tool_metadata.get("vision_model_config")
    if not vision_config:
        # No vision model configured — skip preprocessing.
        return

    # Collect all ImageBlocks with their parent message index and block index
    pending: list[tuple[int, int, ImageBlock]] = []
    for msg_idx, msg in enumerate(messages):
        if msg.role != "user":
            continue
        for blk_idx, block in enumerate(msg.content):
            if isinstance(block, ImageBlock):
                pending.append((msg_idx, blk_idx, block))

    if not pending:
        return

    yield StatusEvent(message=_IMAGE_PREPROCESS_STATUS)

    # Process images in parallel
    async def _describe(msg_idx: int, blk_idx: int, block: ImageBlock) -> tuple[int, int, str]:
        tool = context.tool_registry.get("image_to_text")
        if tool is None:
            return msg_idx, blk_idx, "[Image: could not describe — image_to_text tool not available]"

        # Build tool input
        tool_input_data: dict[str, object] = {
            "image_data": block.data,
            "media_type": block.media_type,
            "prompt": "Describe this image in detail, including any text, "
                      "UI elements, code, diagrams, or visual information present.",
        }

        try:
            parsed = tool.input_model.model_validate(tool_input_data)
        except (ValueError, TypeError):
            return msg_idx, blk_idx, "[Image: could not parse image data]"

        exec_context = ToolExecutionContext(
            cwd=context.cwd,
            metadata={
                "vision_model_config": vision_config,
                **(context.tool_metadata or {}),
            },
        )
        result = await tool.execute(parsed, exec_context)
        if result.is_error:
            return msg_idx, blk_idx, f"[Image description failed: {result.output}]"
        return msg_idx, blk_idx, result.output

    results = await asyncio.gather(*[_describe(mi, bi, blk) for mi, bi, blk in pending])

    # Replace ImageBlocks with TextBlocks in-place
    for msg_idx, blk_idx, description in results:
        msg = messages[msg_idx]
        msg.content[blk_idx] = TextBlock(text=description)
