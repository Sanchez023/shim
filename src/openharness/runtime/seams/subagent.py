"""External subagent port used by the Codex delegation tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CodexSubagentResult:
    """Provider-neutral outcome of one Codex delegation."""

    output: str
    stop_reason: str
    diagnostic: str = ""


@runtime_checkable
class CodexSubagentPort(Protocol):
    """Run one unattended external Codex task."""

    async def run(
        self,
        task: str,
        *,
        cwd: str,
        model: str | None = None,
    ) -> CodexSubagentResult:
        """Run *task* and return its final answer or diagnostic."""
