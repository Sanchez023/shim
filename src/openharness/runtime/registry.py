"""Per-session capability registry and bundle.

:class:`CapabilityBundle` is the injection contract for the engine seams.  Its
nine fields are the *entire* contract: every field is read by ``QueryEngine``
or by the runtime object it builds.  Do not add a field here without a real
consumer in the same change.

Production does not use this bundle.  The interactive runtime
(``openharness.ui.runtime.build_runtime``) passes explicit keyword arguments to
``QueryEngine`` (``event_sink`` / ``event_sequencer`` / ``session_id``) and lets
the engine construct the default seam implementations itself.  The bundle
exists for tests and embedders that need to replace one provider without
editing the query loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openharness.runtime.seams.execution import (
    ApprovalGate,
    ModelClient,
    ToolExecutor,
)


@dataclass
class CapabilityBundle:
    """Resolved providers for one session.

    Every field is optional so a caller can override just the provider under
    test and let the engine fall back to its defaults for the rest.
    """

    model_client: ModelClient | None = None
    tool_executor: ToolExecutor | None = None
    approval_gate: ApprovalGate | None = None
    event_sink: Any | None = None
    event_sequencer: Any | None = None
    event_log: Any | None = None
    context_governor: Any | None = None
    security_policy: Any | None = None
    session_id: str = ""


class CapabilityRegistry:
    """Named providers with reversible registration."""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(
        self,
        name: str,
        provider: Any,
        *,
        replace: bool = True,
    ) -> Callable[[], None]:
        """Register a provider and return a disposer.

        Registration and lifetime are the same operation: disposing restores
        the previous provider (or removes the entry).
        """
        if not replace and name in self._providers:
            raise KeyError(f"capability already registered: {name}")
        previous = self._providers.get(name)
        self._providers[name] = provider

        def dispose() -> None:
            if self._providers.get(name) is not provider:
                return
            if previous is None:
                self._providers.pop(name, None)
            else:
                self._providers[name] = previous

        return dispose

    def resolve(self, name: str, default: Any = None) -> Any:
        return self._providers.get(name, default)

    def require(self, name: str) -> Any:
        if name not in self._providers:
            raise KeyError(f"capability not registered: {name}")
        return self._providers[name]

    def snapshot(self) -> dict[str, Any]:
        return dict(self._providers)
