"""Engine-facing re-exports of the runtime seam ports.

The canonical protocol definitions live in ``openharness.runtime.seams``
(ADR-0010 D2). Keeping this module as a thin namespace lets the engine depend
on a single import site without creating a second port home.
"""

from __future__ import annotations

from openharness.runtime.seams.compact import CompactPort
from openharness.runtime.seams.core import (
    EventLogPort,
    EventLogStore,
    SessionAppendPort,
)
from openharness.runtime.seams.memory import MemoryPort

__all__ = [
    "CompactPort",
    "EventLogPort",
    "EventLogStore",
    "MemoryPort",
    "SessionAppendPort",
]
