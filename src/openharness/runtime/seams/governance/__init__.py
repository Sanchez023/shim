"""Governance seams: security and context.

Exports are resolved on first access; see :mod:`openharness.runtime._lazy` for
why eager re-exports here would create an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openharness.runtime._lazy import make_getattr

if TYPE_CHECKING:
    from openharness.runtime.seams.governance.context import (
        ContextGovernor,
        ContextPlan,
        ExistingCompactGovernor,
        NoCompactGovernor,
    )
    from openharness.runtime.seams.governance.models import (
        SecurityDecision,
        SecurityRequest,
    )
    from openharness.runtime.seams.governance.security import (
        CompositeSecurityPolicy,
        PermissionCheckerPolicy,
        SandboxPolicy,
        SecretAccessPolicy,
    )

__all__ = [
    "CompositeSecurityPolicy",
    "ContextGovernor",
    "ContextPlan",
    "ExistingCompactGovernor",
    "NoCompactGovernor",
    "PermissionCheckerPolicy",
    "SandboxPolicy",
    "SecretAccessPolicy",
    "SecurityDecision",
    "SecurityRequest",
    "build_default_context_governor",
    "build_default_security_policy",
]

_SPEC: dict[str, tuple[str, str]] = {
    "ContextGovernor": ("openharness.runtime.seams.governance.context", "ContextGovernor"),
    "ContextPlan": ("openharness.runtime.seams.governance.context", "ContextPlan"),
    "ExistingCompactGovernor": (
        "openharness.runtime.seams.governance.context",
        "ExistingCompactGovernor",
    ),
    "NoCompactGovernor": ("openharness.runtime.seams.governance.context", "NoCompactGovernor"),
    "build_default_context_governor": (
        "openharness.runtime.seams.governance.context",
        "build_default_context_governor",
    ),
    "SecurityDecision": ("openharness.runtime.seams.governance.models", "SecurityDecision"),
    "SecurityRequest": ("openharness.runtime.seams.governance.models", "SecurityRequest"),
    "CompositeSecurityPolicy": (
        "openharness.runtime.seams.governance.security",
        "CompositeSecurityPolicy",
    ),
    "PermissionCheckerPolicy": (
        "openharness.runtime.seams.governance.security",
        "PermissionCheckerPolicy",
    ),
    "SandboxPolicy": ("openharness.runtime.seams.governance.security", "SandboxPolicy"),
    "SecretAccessPolicy": (
        "openharness.runtime.seams.governance.security",
        "SecretAccessPolicy",
    ),
    "build_default_security_policy": (
        "openharness.runtime.seams.governance.security",
        "build_default_security_policy",
    ),
}

__getattr__ = make_getattr(__name__, _SPEC)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
