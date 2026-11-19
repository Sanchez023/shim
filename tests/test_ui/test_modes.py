"""Tests for UI mode helpers."""

from __future__ import annotations

import sys

import pytest

from openharness.ui.input import InputSession
from openharness.ui.output import OutputRenderer


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="prompt_toolkit requires a real Windows console buffer",
)
def test_input_session_updates_prompt_modes():
    session = InputSession()
    assert session._prompt == "> "

    session.set_modes(vim_enabled=True, voice_enabled=False)
    assert session._prompt == "[vim]> "

    session.set_modes(vim_enabled=True, voice_enabled=True)
    assert session._prompt == "[vim][voice]> "


def test_output_renderer_style_can_change():
    renderer = OutputRenderer()
    assert renderer._style_name == "default"

    renderer.set_style("minimal")
    assert renderer._style_name == "minimal"
