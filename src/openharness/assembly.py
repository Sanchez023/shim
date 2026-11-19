"""Composition root for a runtime session.

This module wires the service-layer assembly to the presentation-layer command
registry. Keeping the command registry here (instead of in ``services``) avoids
a services <-> commands cycle while still giving headless callers a single
entry point that does not import ``openharness.ui``.
"""

from __future__ import annotations

from typing import Any

from openharness.commands import create_default_command_registry
from openharness.services.assembly import (
    RuntimeBundle,
    _last_user_text,
    _resolve_api_client_from_settings,
    close_runtime,
    start_runtime,
)
from openharness.services.assembly import (
    build_runtime as _build_runtime,
)


async def build_runtime(**kwargs: Any) -> RuntimeBundle:
    """Build the shared runtime and attach the default command registry."""
    return await _build_runtime(
        command_registry_factory=create_default_command_registry,
        **kwargs,
    )


__all__ = [
    "RuntimeBundle",
    "_last_user_text",
    "_resolve_api_client_from_settings",
    "build_runtime",
    "close_runtime",
    "start_runtime",
]
