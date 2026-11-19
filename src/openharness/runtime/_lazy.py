"""PEP 562 lazy exports for the runtime packages.

Several runtime packages re-export a wide surface purely for convenience.  When
those re-exports are eager, importing any leaf module drags in every sibling:
``runtime.seams.governance.models`` would pull in the capability registry,
which pulls in ``seams.execution``, which needs the tool dispatcher - a cycle
with no natural seam.  Resolving names on first access keeps the convenience
API while letting leaf modules import in isolation.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


def make_getattr(package: str, spec: dict[str, tuple[str, str]]) -> Callable[[str], Any]:
    """Build a module-level ``__getattr__`` that resolves ``spec`` on demand.

    ``spec`` maps an exported name to ``(module_name, attribute_name)``.  The
    resolved value is written back into the package namespace so each name is
    looked up once.
    """

    def __getattr__(name: str) -> Any:
        try:
            module_name, attribute = spec[name]
        except KeyError:
            raise AttributeError(f"module {package!r} has no attribute {name!r}") from None
        value = getattr(importlib.import_module(module_name), attribute)
        setattr(importlib.import_module(package), name, value)
        return value

    return __getattr__
