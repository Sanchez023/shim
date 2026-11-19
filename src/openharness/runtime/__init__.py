"""Replaceable runtime capabilities (Capability Seams).

Exports are resolved on first access; see :mod:`openharness.runtime._lazy` for
why eager re-exports here would create an import cycle.

``openharness.runtime.assembly`` is intentionally *not* re-exported.  It is
experimental in-process capability assembly and has no production consumer, so
keeping it out of ``__all__`` keeps the public namespace honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openharness.runtime._lazy import make_getattr

if TYPE_CHECKING:
    from openharness.runtime.registry import CapabilityBundle, CapabilityRegistry
    from openharness.runtime.scope import SessionScope

__all__ = ["CapabilityBundle", "CapabilityRegistry", "SessionScope"]

_SPEC: dict[str, tuple[str, str]] = {
    "CapabilityBundle": ("openharness.runtime.registry", "CapabilityBundle"),
    "CapabilityRegistry": ("openharness.runtime.registry", "CapabilityRegistry"),
    "SessionScope": ("openharness.runtime.scope", "SessionScope"),
}

__getattr__ = make_getattr(__name__, _SPEC)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
