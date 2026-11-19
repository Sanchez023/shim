"""Backward-compatible re-exports for usage models.

The canonical definition lives in :mod:`openharness.domain.usage`
(ADR-0010 D2).
"""

from __future__ import annotations

from openharness.domain.usage import UsageSnapshot

__all__ = ["UsageSnapshot"]
