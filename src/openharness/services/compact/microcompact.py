"""Cheap tool-result compaction that needs no LLM call."""

from __future__ import annotations

import logging

from openharness.domain.messages import (
    ContentBlock,
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.services.compact.constants import (
    COMPACTABLE_TOOLS,
    DEFAULT_KEEP_RECENT,
    TIME_BASED_MC_CLEARED_MESSAGE,
)
from openharness.services.compact.tokens import estimate_image_tokens
from openharness.utils.tokens import estimate_tokens
from openharness.utils.tool_outputs import is_microcompactable_tool_result

log = logging.getLogger(__name__)

#: Prefix the engine uses for the caption that names the tool an image came
#: from (see ``engine.step_runner._take_tool_images``).
IMAGE_CAPTION_PREFIX = "[image from "


def _is_orphan_image_caption(
    content: list[ContentBlock],
    index: int,
    clear_set: set[str],
) -> bool:
    """Return True when a caption belongs to an image that is being dropped."""
    block = content[index]
    if not isinstance(block, TextBlock) or not block.text.startswith(IMAGE_CAPTION_PREFIX):
        return False
    following = content[index + 1] if index + 1 < len(content) else None
    return isinstance(following, ImageBlock) and following.origin_tool_use_id in clear_set

def _collect_compactable_tool_ids(messages: list[ConversationMessage]) -> list[str]:
    """Walk messages and collect tool_use IDs whose results are compactable."""
    ordered_ids: list[str] = []
    tool_names: dict[str, str] = {}
    result_content: dict[str, str] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                ordered_ids.append(block.id)
                tool_names[block.id] = block.name
            elif isinstance(block, ToolResultBlock):
                result_content[block.tool_use_id] = block.content
    return [
        tool_id
        for tool_id in ordered_ids
        if tool_names.get(tool_id, "") in COMPACTABLE_TOOLS
        or is_microcompactable_tool_result(
            tool_names.get(tool_id, ""),
            result_content.get(tool_id, ""),
        )
    ]


def microcompact_messages(
    messages: list[ConversationMessage],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[ConversationMessage], int]:
    """Clear old compactable tool results, keeping the most recent *keep_recent*.

    This is the cheap first pass — no LLM call required. Tool result content
    is replaced with :data:`TIME_BASED_MC_CLEARED_MESSAGE`.

    Returns:
        (messages, tokens_saved) — messages are mutated in place for efficiency.
    """
    keep_recent = max(1, keep_recent)  # never clear ALL results
    all_ids = _collect_compactable_tool_ids(messages)

    if len(all_ids) <= keep_recent:
        return messages, 0

    keep_set = set(all_ids[-keep_recent:])
    clear_set = set(all_ids) - keep_set

    tokens_saved = 0
    for msg in messages:
        if msg.role != "user":
            continue
        new_content: list[ContentBlock] = []
        for index, block in enumerate(msg.content):
            if _is_orphan_image_caption(msg.content, index, clear_set):
                continue
            if isinstance(block, ImageBlock) and block.origin_tool_use_id in clear_set:
                # Keep the image and its caption in lockstep with the tool
                # result they came from; a screenshot of a cleared result is
                # dead weight in every later request.
                tokens_saved += estimate_image_tokens()
                continue
            if (
                isinstance(block, ToolResultBlock)
                and block.tool_use_id in clear_set
                and block.content != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                tokens_saved += estimate_tokens(block.content)
                new_content.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=TIME_BASED_MC_CLEARED_MESSAGE,
                        is_error=block.is_error,
                    )
                )
            else:
                new_content.append(block)
        msg.content = new_content

    if tokens_saved > 0:
        log.info("Microcompact cleared %d tool results, saved ~%d tokens", len(clear_set), tokens_saved)

    return messages, tokens_saved
