"""Backward-compatible re-exports for tool-output budget helpers.

The implementation lives in :mod:`openharness.utils.tool_outputs`.
"""

from __future__ import annotations

from openharness.utils.tool_outputs import (
    DEFAULT_MICROCOMPACT_TOOL_RESULT_CHARS,
    DEFAULT_TOOL_OUTPUT_INLINE_CHARS,
    DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS,
    is_microcompactable_tool_result,
    microcompact_tool_result_chars,
    tool_output_inline_chars,
    tool_output_preview_chars,
)

__all__ = [
    "DEFAULT_MICROCOMPACT_TOOL_RESULT_CHARS",
    "DEFAULT_TOOL_OUTPUT_INLINE_CHARS",
    "DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS",
    "is_microcompactable_tool_result",
    "microcompact_tool_result_chars",
    "tool_output_inline_chars",
    "tool_output_preview_chars",
]
