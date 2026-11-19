"""Which tools the model sees, and in what order.

The ordering rule is a cache invariant, not a style preference (ADR-0008):
the tool schema list sits inside the provider's cached prefix, so a deferred
tool that has been activated must be **appended** to the tail.  Inserting it
where the registry happens to keep it would invalidate every cached token
after the tools block.

This module owns that rule so ``QueryEngine`` cannot re-sort by accident.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from openharness.tools.base import ToolRegistry
from openharness.tools.profiles import (
    DEFAULT_TOOL_PROFILE,
    normalize_tool_profile,
    visible_tool_names,
)


class ToolVisibility:
    """Track the active tool names and the deferred-tool append order."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        profile: str = DEFAULT_TOOL_PROFILE,
    ) -> None:
        self._registry = registry
        self._profile = normalize_tool_profile(profile)
        self._loaded: set[str] = set()
        # Deferred tools are appended to the wire schema in activation order.
        # Never insert a newly activated tool into the middle of the list: that
        # would invalidate the provider's cached tool prefix.
        self._loaded_order: list[str] = []
        self._active: set[str] | None = visible_tool_names(
            registry,
            self._profile,
            loaded=self._loaded,
        )

    @property
    def profile(self) -> str:
        """Return the normalized profile name."""
        return self._profile

    @property
    def active_names(self) -> set[str] | None:
        """Return the live active-name set (``None`` means "all tools").

        The set is mutated in place by :meth:`activate` so a turn that is
        already holding a reference sees the newly activated tool immediately.
        """
        return self._active

    @property
    def append_order(self) -> tuple[str, ...]:
        """Return the deferred-tool append order for the current profile."""
        if self._active is None:
            return ()
        return tuple(self._loaded_order)

    def schemas(self) -> list[dict[str, Any]]:
        """Return the tool schemas that would be sent to the model now."""
        return self._registry.to_api_schema(self._active, append_order=self.append_order)

    def activate(self, names: Iterable[str]) -> None:
        """Enable deferred tools for the rest of the session (append-only)."""
        if self._active is None:
            return
        for name in names:
            if self._registry.get(name) is None:
                continue
            if name in self._active:
                continue
            self._loaded.add(name)
            self._active.add(name)
            self._loaded_order.append(name)

    def set_profile(self, profile: str) -> str:
        """Switch the visibility profile and return the normalized name."""
        self._profile = normalize_tool_profile(profile)
        self._active = visible_tool_names(
            self._registry,
            self._profile,
            loaded=self._loaded,
        )
        return self._profile
