"""In-process capability assembly.

Experimental: not exported from :mod:`openharness.runtime`.  This is
deliberately smaller than Cordis: a module declares requirements, registers
providers through a :class:`CapabilityRegistry`, and returns a disposer.  The
assembler keeps installation order and unwinds in reverse.

The name ``assembly`` (rather than ``plugin``) is intentional: the
Markdown/process plugin system under ``openharness.plugins`` is a separate
concept, and this module is not part of the public runtime namespace.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openharness.runtime.registry import CapabilityRegistry


@dataclass(frozen=True)
class CapabilityModule:
    """One installable unit of runtime capability."""

    name: str
    version: str = "0.1.0"
    requires: tuple[str, ...] = ()
    setup: Callable[[CapabilityRegistry], Callable[[], None] | None] | None = None


@dataclass
class _Installed:
    module: CapabilityModule
    disposers: list[Callable[[], None]] = field(default_factory=list)


class CapabilityAssembler:
    """Install modules in dependency order and dispose them in reverse."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._installed: dict[str, _Installed] = {}
        self._order: list[str] = []

    @property
    def installed(self) -> list[str]:
        return list(self._order)

    def install(self, module: CapabilityModule) -> None:
        if module.name in self._installed:
            raise ValueError(f"module already installed: {module.name}")
        missing = [name for name in module.requires if name not in self._installed]
        if missing:
            raise ValueError(
                f"module {module.name} requires missing modules: {', '.join(missing)}"
            )
        disposers: list[Callable[[], None]] = []
        if module.setup is not None:
            disposer = module.setup(self._registry)
            if disposer is not None:
                disposers.append(disposer)
        self._installed[module.name] = _Installed(module, disposers)
        self._order.append(module.name)

    def install_all(self, modules: list[CapabilityModule]) -> None:
        """Install in dependency order, rejecting cycles and missing deps."""
        pending = {module.name: module for module in modules}
        while pending:
            progressed = False
            for name, module in list(pending.items()):
                if all(requirement in self._installed for requirement in module.requires):
                    self.install(module)
                    pending.pop(name)
                    progressed = True
            if not progressed:
                raise ValueError(
                    "cannot resolve module dependencies: "
                    + ", ".join(sorted(pending))
                )

    def uninstall(self, name: str) -> None:
        if name not in self._installed:
            raise KeyError(f"module not installed: {name}")
        dependents = [
            other
            for other, record in self._installed.items()
            if name in record.module.requires
        ]
        if dependents:
            raise ValueError(
                f"module {name} is required by: {', '.join(sorted(dependents))}"
            )
        record = self._installed.pop(name)
        for disposer in reversed(record.disposers):
            disposer()
        self._order.remove(name)

    def uninstall_all(self) -> None:
        for name in list(reversed(self._order)):
            self.uninstall(name)

    def get(self, name: str) -> CapabilityModule:
        return self._installed[name].module

    def registry_snapshot(self) -> dict[str, Any]:
        return self._registry.snapshot()
