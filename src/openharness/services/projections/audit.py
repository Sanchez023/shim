"""Projection for governance decisions.

Audit records are derived from ``permission.decided`` events.  Sensitive
provider state is never present in those events, but the redaction helper is
defence in depth for future event kinds.
"""

from __future__ import annotations

from typing import Any

from openharness.domain.events import AgentEvent

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "encrypted_content",
    "password",
    "raw",
    "secret",
    "token",
}

_MAX_RECORDS = 10_000


class AuditState:
    """A bounded, append-only view of security decisions."""

    def __init__(self, max_records: int = _MAX_RECORDS) -> None:
        self._records: list[dict[str, Any]] = []
        self._max_records = max(1, max_records)
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def apply(self, event: AgentEvent) -> None:
        self._last_seq = max(self._last_seq, event.seq)
        if event.kind != "permission.decided":
            return
        record = {
            "seq": event.seq,
            "ts": event.ts,
            "tool_name": str(event.payload.get("tool_name") or ""),
            "dimension": str(event.payload.get("dimension") or ""),
            "decision": str(event.payload.get("decision") or ""),
            "reason": str(event.payload.get("reason") or ""),
            "detail": _redact(event.payload.get("detail")),
        }
        self._records.append(record)
        if len(self._records) > self._max_records:
            del self._records[: len(self._records) - self._max_records]

    def snapshot(self, as_of_seq: int | None = None) -> list[dict[str, Any]]:
        if as_of_seq is None:
            return [dict(record) for record in self._records]
        return [dict(record) for record in self._records if record["seq"] <= as_of_seq]

    def reset(self) -> None:
        self._records = []
        self._last_seq = 0


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value]
    return value
