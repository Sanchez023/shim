"""Compaction progress events and checkpoint bookkeeping."""

from __future__ import annotations

from typing import Any, Literal

from openharness.domain.stream_events import CompactProgressEvent
from openharness.services.compact.boundaries import _sanitize_metadata
from openharness.services.compact.constants import CompactProgressCallback, CompactTrigger


def _record_compact_checkpoint(
    carryover_metadata: dict[str, Any] | None,
    *,
    checkpoint: str,
    trigger: CompactTrigger,
    message_count: int,
    token_count: int,
    attempt: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "trigger": trigger,
        "message_count": message_count,
        "token_count": token_count,
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if details:
        payload.update(_sanitize_metadata(details))
    if carryover_metadata is not None:
        checkpoints = carryover_metadata.setdefault("compact_checkpoints", [])
        if isinstance(checkpoints, list):
            checkpoints.append(payload)
        carryover_metadata["compact_last"] = payload
    return payload


async def _emit_progress(
    callback: CompactProgressCallback | None,
    *,
    phase: Literal[
        "hooks_start",
        "context_collapse_start",
        "context_collapse_end",
        "session_memory_start",
        "session_memory_end",
        "compact_start",
        "compact_retry",
        "compact_end",
        "compact_failed",
    ],
    trigger: CompactTrigger,
    message: str | None = None,
    attempt: int | None = None,
    checkpoint: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if callback is None:
        return
    await callback(
        CompactProgressEvent(
            phase=phase,
            trigger=trigger,
            message=message,
            attempt=attempt,
            checkpoint=checkpoint,
            metadata=_sanitize_metadata(metadata) if metadata else None,
        )
    )
