"""Permission checker plus the security policy derived from it.

The subtle part is the coupling: the default security policy captures the
checker **by reference**, so swapping the checker without rebuilding the policy
silently keeps enforcing the old mode and rules (ADR-0004).  That invariant is
easy to break in a refactor, so it lives in one place here instead of inline in
``QueryEngine``.
"""

from __future__ import annotations

from collections.abc import Callable

from openharness.permissions import PermissionMode
from openharness.permissions.checker import PermissionChecker
from openharness.runtime.seams.governance.security import build_default_security_policy


class PermissionPolicy:
    """Own the live checker, its mode, and the matching security policy."""

    def __init__(
        self,
        checker: PermissionChecker,
        *,
        security_policy: object | None = None,
        on_mode_changed: Callable[[PermissionMode], None] | None = None,
    ) -> None:
        self._checker = checker
        self._explicit_policy = security_policy
        self._security_policy = (
            build_default_security_policy(checker)
            if security_policy is None
            else security_policy
        )
        self._on_mode_changed = on_mode_changed

    @property
    def checker(self) -> PermissionChecker:
        """Return the live permission checker."""
        return self._checker

    @property
    def security_policy(self) -> object | None:
        """Return the policy the current turn evaluates against."""
        return self._security_policy

    @property
    def mode(self) -> PermissionMode:
        """Return the live permission mode used by the current turn."""
        return self._checker.mode

    def set_checker(self, checker: PermissionChecker) -> None:
        """Replace the checker, rebuilding the default policy to match."""
        self._checker = checker
        if self._explicit_policy is None:
            self._security_policy = build_default_security_policy(checker)
        self._notify(checker.mode)

    def set_mode(self, mode: PermissionMode) -> None:
        """Switch the live permission mode, including the current turn."""
        self._checker.set_mode(mode)
        self._notify(mode)

    def _notify(self, mode: PermissionMode) -> None:
        if self._on_mode_changed is not None:
            self._on_mode_changed(mode)
