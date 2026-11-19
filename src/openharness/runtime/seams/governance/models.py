"""Value objects for the security seam.

These live in their own leaf module on purpose.  The tool dispatcher needs to
build a request and read a decision, but importing ``governance.security``
pulls in ``openharness.runtime``, whose package ``__init__`` eagerly imports the
capability registry - and the registry imports the seam that the dispatcher is
implemented for.  Keeping the data types free of imports breaks that cycle at
the boundary instead of hiding it behind a function-local import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecurityRequest:
    """Everything a policy may inspect for one tool call."""

    tool_name: str
    is_read_only: bool
    file_path: str | None = None
    command: str | None = None
    # Path-looking tokens recovered from ``command`` when the tool exposes no
    # ``file_path`` (for example the bash tool).
    paths: tuple[str, ...] = ()
    # ``"read_only"`` / ``"mutating"`` / ``"unknown"`` when the tool can
    # classify the invocation, ``None`` when the tool has nothing finer to say
    # than ``is_read_only``.  Plan mode needs the difference: "unknown" should
    # ask the user, "mutating" should be refused (ADR-0013 D2).
    read_only_verdict: str | None = None
    #: Machine-readable reason behind ``read_only_verdict`` (``write_flag``,
    #: ``command_substitution``, …), kept for the audit projection.
    read_only_reason: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityDecision:
    """Result of one policy dimension."""

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""
    dimension: str = "tool"


__all__ = ["SecurityDecision", "SecurityRequest"]
