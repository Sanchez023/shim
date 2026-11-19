"""Declarative model-provider capabilities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model adapter can accept or preserve."""

    streaming: bool = True
    tools: bool = True
    images: bool = False
    reasoning: bool = False
    native_reasoning_state: bool = False
    #: Provider caches prompt prefixes automatically (no cache_control blocks).
    implicit_prompt_cache: bool = False
