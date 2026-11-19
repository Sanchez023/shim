"""Permission checking for tool execution."""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from openharness.config.settings import PermissionSettings
from openharness.permissions.modes import PermissionMode

log = logging.getLogger(__name__)

# Paths that are always denied regardless of permission mode or user config.
# These protect high-value credential and key material from LLM-directed access
# (including via prompt injection).  Patterns use fnmatch syntax and are matched
# against the fully-resolved absolute path produced by the query engine.
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # SSH keys and config
    "*/.ssh/*",
    # AWS credentials
    "*/.aws/credentials",
    "*/.aws/config",
    # GCP credentials
    "*/.config/gcloud/*",
    # Azure credentials
    "*/.azure/*",
    # GPG keys
    "*/.gnupg/*",
    # Docker credentials
    "*/.docker/config.json",
    # Kubernetes credentials
    "*/.kube/config",
    # OpenHarness own credential stores
    "*/.openharness/credentials.json",
    "*/.openharness/copilot_auth.json",
    # Harness state that grants privileges: writing permission.mode here would
    # persist an escalation past the end of the session.
    "*/.openharness/settings.json",
    "*/.openharness/sessions/*",
    # Package-registry and HTTP credentials
    "*/.netrc",
    "*/.pypirc",
    "*/.npmrc",
    "*/.config/gh/*",
)


@dataclass(frozen=True)
class PermissionDecision:
    """Result of checking whether a tool invocation may run."""

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PathRule:
    """A glob-based path permission rule."""

    pattern: str
    allow: bool  # True = allow, False = deny


# Plan mode is read-only for the workspace, but recording the plan itself is
# part of planning: the sidebar renders this checklist.
_PLAN_MODE_TRACKING_TOOLS = frozenset({"todo_write"})
_PLAN_MODE_SWITCH_TOOLS = frozenset({"exit_plan_mode", "enter_plan_mode"})


class PermissionChecker:
    """Evaluate tool usage against the configured permission mode and rules."""

    def __init__(self, settings: PermissionSettings) -> None:
        self._settings = settings
        # Parse path rules from settings
        self._path_rules: list[PathRule] = []
        for rule in getattr(settings, "path_rules", []):
            pattern = getattr(rule, "pattern", None) or (rule.get("pattern") if isinstance(rule, dict) else None)
            allow = getattr(rule, "allow", True) if not isinstance(rule, dict) else rule.get("allow", True)
            if isinstance(pattern, str) and pattern.strip():
                self._path_rules.append(PathRule(pattern=pattern.strip(), allow=allow))
            else:
                log.warning(
                    "Skipping path rule with missing, empty, or non-string 'pattern' field: %r",
                    rule,
                )

    @property
    def mode(self) -> PermissionMode:
        """Return the live permission mode."""
        return self._settings.mode

    def set_mode(self, mode: PermissionMode) -> None:
        """Update the live permission mode in place.

        Tool dispatch holds a reference to this checker for the whole turn, so
        changing the object (rather than replacing it on the engine) lets
        ``exit_plan_mode`` take effect immediately for the remaining tools in
        the same turn.
        """
        self._settings.mode = mode

    def evaluate(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        file_path: str | None = None,
        command: str | None = None,
        paths: tuple[str, ...] = (),
        workspace_root: str | None = None,
        read_only_verdict: str | None = None,
    ) -> PermissionDecision:
        """Return whether the tool may run immediately.

        ``paths`` carries path-looking tokens recovered from a shell command
        when the tool has no ``file_path`` field (the bash tool).  They
        participate in the same built-in credential-path protection.

        ``read_only_verdict`` distinguishes "proven to mutate" from "could not
        be proven read-only"; plan mode refuses the former and asks for the
        latter (ADR-0013 D2).
        """
        # Built-in sensitive path protection — always active, cannot be
        # overridden by user settings or permission mode.  This is a
        # defence-in-depth measure against LLM-directed or prompt-injection
        # driven access to credential files.
        if file_path:
            for candidate_path in _policy_match_paths(file_path):
                for pattern in SENSITIVE_PATH_PATTERNS:
                    if fnmatch.fnmatch(candidate_path, pattern):
                        return PermissionDecision(
                            allowed=False,
                            reason=(
                                f"Access denied: {file_path} is a sensitive credential path "
                                f"(matched built-in pattern '{pattern}')"
                            ),
                        )

        # Explicit tool deny list
        if tool_name in self._settings.denied_tools:
            return PermissionDecision(allowed=False, reason=f"{tool_name} is explicitly denied")

        # Bash has no file_path, so inspect paths recovered from the command.
        # In default mode the ask path is preserved (the command is not
        # read-only); every other mode has no prompt to rely on, so deny.
        if not file_path and paths:
            for path in paths:
                if _command_path_is_sensitive(path):
                    if self._settings.mode == PermissionMode.DEFAULT:
                        return PermissionDecision(
                            allowed=False,
                            requires_confirmation=True,
                            reason=(
                                f"Access to the sensitive credential path {path} "
                                "requires confirmation."
                            ),
                        )
                    return PermissionDecision(
                        allowed=False,
                        reason=(
                            f"Access denied: {path} is a sensitive credential path "
                            "(matched a built-in pattern)"
                        ),
                    )

        # Explicit tool allow list
        if tool_name in self._settings.allowed_tools:
            return PermissionDecision(allowed=True, reason=f"{tool_name} is explicitly allowed")

        # Check path-level rules
        if file_path and self._path_rules:
            for candidate_path in _policy_match_paths(file_path):
                for rule in self._path_rules:
                    if fnmatch.fnmatch(candidate_path, rule.pattern):
                        if not rule.allow:
                            return PermissionDecision(
                                allowed=False,
                                reason=f"Path {file_path} matches deny rule: {rule.pattern}",
                            )

        # Check command deny patterns (e.g. deny "rm -rf /")
        if command:
            for pattern in getattr(self._settings, "denied_commands", []):
                if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                    return PermissionDecision(
                        allowed=False,
                        reason=f"Command matches deny pattern: {pattern}",
                    )

        # Full auto: allow everything
        if self._settings.mode == PermissionMode.FULL_AUTO:
            return PermissionDecision(allowed=True, reason="Auto mode allows all tools")

        # Read-only tools always allowed
        if is_read_only:
            return PermissionDecision(allowed=True, reason="read-only tools are allowed")

        # Plan mode: block mutating tools, but allow the plan's own checklist
        # and let the agent *request* leaving plan mode (the user confirms).
        if self._settings.mode == PermissionMode.PLAN:
            if tool_name in _PLAN_MODE_TRACKING_TOOLS:
                return PermissionDecision(
                    allowed=True,
                    reason="Plan mode allows todo tracking writes",
                )
            if tool_name == "enter_plan_mode":
                return PermissionDecision(
                    allowed=True,
                    reason="Plan mode is already active",
                )
            if tool_name in _PLAN_MODE_SWITCH_TOOLS:
                return PermissionDecision(
                    allowed=False,
                    requires_confirmation=True,
                    reason="Switching out of plan mode requires user confirmation",
                )
            if read_only_verdict == "unknown":
                # Plan mode's constraint is "do not modify the workspace", not
                # "never ask".  Refusing outright pushed the model to rewrite a
                # readable command into one that is harder to classify, so the
                # unprovable case asks instead (ADR-0013 D2).
                return PermissionDecision(
                    allowed=False,
                    requires_confirmation=True,
                    reason=(
                        "Plan mode could not prove this command is read-only "
                        f"({tool_name}). Approve it to run the inspection, or "
                        "rewrite it without command substitution, xargs, or "
                        "find -exec so it can be classified."
                    ),
                )
            return PermissionDecision(
                allowed=False,
                reason=(
                    "Plan mode is read-only: workspace-mutating tools stay blocked. "
                    "Read-only tools, todo_write and Explore/Plan subagents remain "
                    "available. When the plan is ready, call exit_plan_mode (the user "
                    "confirms) or ask the user to switch out of plan mode via Shift+Tab "
                    "or /permissions."
                ),
            )

        # Default mode: require confirmation for mutating tools.  Writes that
        # escape the workspace get a specific reason so the user sees *why* the
        # prompt is worth reading.
        bash_hint = _bash_permission_hint(command)
        reason = (
            "Mutating tools require user confirmation in default mode. "
            "Approve the prompt when asked, or run /permissions full_auto "
            "if you want to allow them for this session."
        )
        if (
            file_path
            and workspace_root
            and not _path_is_within(file_path, workspace_root)
        ):
            reason = (
                f"{file_path} is outside the workspace ({workspace_root}); "
                "writes outside the workspace require explicit confirmation."
            )
        if bash_hint:
            reason = f"{reason} {bash_hint}"
        return PermissionDecision(
            allowed=False,
            requires_confirmation=True,
            reason=reason,
        )


def _path_is_within(path: str, root: str) -> bool:
    """True when *path* is *root* or lives underneath it (both resolved)."""
    try:
        candidate = Path(path).resolve() if not os.path.isabs(path) else Path(path)
        base = Path(root).resolve() if not os.path.isabs(root) else Path(root)
    except OSError:
        return False
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _command_path_is_sensitive(path: str) -> bool:
    """Check a path-looking command token against the built-in patterns.

    Imported lazily because ``command_safety`` imports this module at load time.
    """
    from openharness.permissions.command_safety import _is_sensitive_path

    return _is_sensitive_path(os.path.expanduser(path))


def _policy_match_paths(file_path: str) -> tuple[str, ...]:
    """Return path forms that should participate in policy matching.

    Directory-scoped tools like ``grep`` and ``glob`` may operate on a root such
    as ``/home/user/.ssh``. Appending a trailing slash lets glob-style deny
    patterns like ``*/.ssh/*`` and ``/etc/*`` match the directory root itself.
    """
    normalized = file_path.rstrip("/")
    if not normalized:
        return (file_path,)
    return (normalized, normalized + "/")


def _bash_permission_hint(command: str | None) -> str:
    if not command:
        return ""
    lowered = command.lower()
    install_markers = (
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "pip install",
        "uv pip install",
        "poetry install",
        "cargo install",
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "npx create-",
        "npm init ",
        "pnpm init ",
        "yarn init ",
    )
    if any(marker in lowered for marker in install_markers):
        return (
            "Package installation and scaffolding commands change the workspace, "
            "so they will not run automatically in default mode."
        )
    return ""
