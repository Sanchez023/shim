"""Small CLI helpers shared by command modules."""

from __future__ import annotations


def _safe_short(text: str, *, limit: int = 140) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
