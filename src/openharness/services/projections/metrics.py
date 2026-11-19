"""Projection for lightweight runtime metrics."""

from __future__ import annotations

from typing import Any

from openharness.domain.events import AgentEvent


class MetricsState:
    """Counters derived from the event stream."""

    def __init__(self) -> None:
        self._last_seq = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._compactions = 0
        self._turns = 0
        self._errors = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def apply(self, event: AgentEvent) -> None:
        self._last_seq = max(self._last_seq, event.seq)
        kind = event.kind

        if kind == "assistant.message":
            usage = event.payload.get("usage")
            if isinstance(usage, dict):
                self._input_tokens += int(usage.get("input_tokens") or 0)
                self._output_tokens += int(usage.get("output_tokens") or 0)
        elif kind == "tool.call":
            self._tool_calls += 1
        elif kind == "tool.result":
            if event.payload.get("is_error"):
                self._tool_errors += 1
        elif kind == "context.compacted":
            self._compactions += 1
        elif kind == "turn.end":
            self._turns += 1
        elif kind == "error":
            self._errors += 1

    def snapshot(self, as_of_seq: int | None = None) -> dict[str, Any]:
        del as_of_seq
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": self._input_tokens + self._output_tokens,
            "tool_calls": self._tool_calls,
            "tool_errors": self._tool_errors,
            "compactions": self._compactions,
            "turns": self._turns,
            "errors": self._errors,
        }

    def reset(self) -> None:
        self._last_seq = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._compactions = 0
        self._turns = 0
        self._errors = 0
