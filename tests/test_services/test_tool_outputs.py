"""Tests for the tool-output context budget."""

from __future__ import annotations

from openharness.services.tool_outputs import (
    DEFAULT_TOOL_OUTPUT_INLINE_CHARS,
    tool_output_inline_chars,
    tool_output_preview_chars,
)


def test_default_inline_limit_is_8k(monkeypatch):
    monkeypatch.delenv("OPENHARNESS_TOOL_OUTPUT_INLINE_CHARS", raising=False)

    assert DEFAULT_TOOL_OUTPUT_INLINE_CHARS == 8_000
    assert tool_output_inline_chars() == 8_000


def test_inline_limit_env_override(monkeypatch):
    monkeypatch.setenv("OPENHARNESS_TOOL_OUTPUT_INLINE_CHARS", "4096")

    assert tool_output_inline_chars() == 4_096


def test_preview_limit_stays_at_3k(monkeypatch):
    monkeypatch.delenv("OPENHARNESS_TOOL_OUTPUT_PREVIEW_CHARS", raising=False)

    assert tool_output_preview_chars() == 3_000
