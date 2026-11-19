"""Multimodal capability detection.

``is_model_multimodal`` decides whether images go to the provider untouched or
through the configured image-to-text fallback model.  A false negative silently
costs an extra model round trip per image, so the model families we actually
run against are pinned here.
"""

from __future__ import annotations

import pytest

from openharness.api.provider import is_model_multimodal


@pytest.mark.parametrize(
    "model",
    [
        # The gateway id for the current DeepSeek v4 flash endpoint.
        "deepreasoning-ds-v4.1flash",
        "deepseek-v4-flash-202605",
        "deepseek-v3.2",
        # Provider prefixes are stripped before matching.
        "deepseek/deepseek-v4-flash",
        # Longer-standing multimodal families stay detected.
        "claude-sonnet-4",
        "gpt-4o",
        "qwen2.5-vl-72b",
        "deepseek-vl2",
    ],
)
def test_known_multimodal_models(model: str) -> None:
    assert is_model_multimodal(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v2.5",
        "gpt-3.5-turbo",
    ],
)
def test_text_only_models_are_not_claimed_as_multimodal(model: str) -> None:
    assert is_model_multimodal(model) is False
