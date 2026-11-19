"""Security dimensions with explicit composition.

The default policy adapts the existing ``PermissionChecker`` so behaviour is
unchanged.  Sandbox, approval and secret-access dimensions can be added
without editing the query loop.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from openharness.permissions.checker import (
    SENSITIVE_PATH_PATTERNS,
    PermissionChecker,
    _policy_match_paths,
)
from openharness.permissions.modes import PermissionMode
from openharness.runtime.seams.governance.models import (
    SecurityDecision,
    SecurityRequest,
)

__all__ = [
    "SecurityDecision",
    "SecurityPolicy",
    "SecurityRequest",
]


@runtime_checkable
class SecurityPolicy(Protocol):
    """A replaceable security dimension."""

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        """Evaluate one tool call."""


class PermissionCheckerPolicy:
    """Adapter from the legacy permission checker to the policy seam."""

    def __init__(self, checker: PermissionChecker) -> None:
        self._checker = checker

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        decision = self._checker.evaluate(
            request.tool_name,
            is_read_only=request.is_read_only,
            file_path=request.file_path,
            command=request.command,
            paths=request.paths,
            read_only_verdict=request.read_only_verdict,
        )
        return SecurityDecision(
            allowed=decision.allowed,
            requires_confirmation=decision.requires_confirmation,
            reason=decision.reason,
            dimension="tool",
        )


class SecretAccessPolicy:
    """Deny reads of credential/key material regardless of permission mode.

    Paths recovered from a shell command (``SecurityRequest.paths``) are denied
    here too.  ``defer_command_paths`` lets the default bundle preserve the
    existing default-mode confirmation prompt: when it returns True the hard
    denial is skipped and the ``PermissionCheckerPolicy`` dimension decides.
    """

    def __init__(
        self,
        patterns: tuple[str, ...] = SENSITIVE_PATH_PATTERNS,
        *,
        defer_command_paths: Callable[[], bool] | None = None,
    ) -> None:
        self._patterns = patterns
        self._defer_command_paths = defer_command_paths

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        if request.file_path:
            candidates = _policy_match_paths(request.file_path)
            from_command = False
        elif request.paths:
            candidates = tuple(os.path.expanduser(path) for path in request.paths)
            from_command = True
        else:
            return SecurityDecision(allowed=True, dimension="secret")

        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            for pattern in self._patterns:
                if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(
                    candidate, pattern
                ):
                    if (
                        from_command
                        and self._defer_command_paths is not None
                        and self._defer_command_paths()
                    ):
                        # Leave the decision to the tool dimension, which asks
                        # for confirmation in default mode.
                        continue
                    return SecurityDecision(
                        allowed=False,
                        reason=(
                            f"Access denied: {candidate} is a sensitive "
                            f"credential path (matched built-in pattern '{pattern}')"
                        ),
                        dimension="secret",
                    )
        return SecurityDecision(allowed=True, dimension="secret")


class SandboxPolicy:
    """Path/network/exec policy seam.

    The default provider is a no-op because the existing sandbox runtime still
    enforces boundaries at execution time.  Replace this provider to move those
    checks into the policy chain.
    """

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        del request
        return SecurityDecision(allowed=True, dimension="sandbox")


class AllowAllSecurityPolicy:
    """Placeholder provider for dimensions enforced elsewhere."""

    def __init__(self, dimension: str) -> None:
        self._dimension = dimension

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        del request
        return SecurityDecision(allowed=True, dimension=self._dimension)


class CompositeSecurityPolicy:
    """Combine dimensions with a deterministic precedence.

    ``hard deny > sandbox deny > explicit deny > ask > allow`` is expressed by
    evaluating every policy and letting denial win over confirmation, which in
    turn wins over allowance.
    """

    def __init__(self, policies: list[SecurityPolicy]) -> None:
        self._policies = list(policies)

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        decisions = [await policy.evaluate(request) for policy in self._policies]
        deny = next(
            (
                decision
                for decision in decisions
                if not decision.allowed and not decision.requires_confirmation
            ),
            None,
        )
        if deny is not None:
            return deny
        ask = next(
            (decision for decision in decisions if decision.requires_confirmation),
            None,
        )
        if ask is not None:
            return ask
        return SecurityDecision(allowed=True, dimension="composite")


def build_default_security_policy(checker: PermissionChecker) -> CompositeSecurityPolicy:
    """Preserve legacy behaviour while exposing every policy dimension."""
    return CompositeSecurityPolicy(
        [
            SecretAccessPolicy(
                defer_command_paths=lambda: checker.mode == PermissionMode.DEFAULT,
            ),
            SandboxPolicy(),
            PermissionCheckerPolicy(checker),
        ]
    )
