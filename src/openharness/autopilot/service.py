"""Project-level repo autopilot state, intake, and execution helpers.

The implementation is split into mixins under :mod:`openharness.autopilot`;
this module composes :class:`RepoAutopilotStore` and keeps the process
plumbing together so callers (and tests) can patch ``subprocess`` in one place.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from openharness.autopilot.dashboard import DashboardMixin
from openharness.autopilot.execution import ExecutionMixin
from openharness.autopilot.github import GitHubMixin
from openharness.autopilot.helpers import (
    _bilingual_lines,
    _json_default,
    _looks_available,
    _parse_verification_entry,
    _safe_text,
    _shorten,
    _source_ref_number,
    _split_verification_argv,
    _VerificationCommand,
)
from openharness.autopilot.policies import (
    _DEFAULT_AUTOPILOT_POLICY,
    _DEFAULT_RELEASE_POLICY,
    _DEFAULT_VERIFICATION_POLICY,
)
from openharness.autopilot.store import StoreMixin
from openharness.autopilot.types import (
    RepoAutopilotRegistry,
    RepoJournalEntry,
    RepoRunResult,
    RepoTaskCard,
    RepoTaskSource,
    RepoTaskStatus,
    RepoVerificationStep,
)
from openharness.autopilot.verification import VerificationMixin

__all__ = [
    "_DEFAULT_AUTOPILOT_POLICY",
    "_DEFAULT_RELEASE_POLICY",
    "_DEFAULT_VERIFICATION_POLICY",
    "RepoAutopilotRegistry",
    "RepoAutopilotStore",
    "RepoJournalEntry",
    "RepoRunResult",
    "RepoTaskCard",
    "RepoTaskSource",
    "RepoTaskStatus",
    "RepoVerificationStep",
    "_VerificationCommand",
    "_bilingual_lines",
    "_json_default",
    "_looks_available",
    "_parse_verification_entry",
    "_safe_text",
    "_shorten",
    "_source_ref_number",
    "_split_verification_argv",
]
from openharness.config.paths import (
    get_project_active_repo_context_path,
    get_project_autopilot_registry_path,
    get_project_autopilot_runs_dir,
    get_project_repo_journal_path,
)


class RepoAutopilotStore(
    ExecutionMixin,
    GitHubMixin,
    StoreMixin,
    VerificationMixin,
    DashboardMixin,
):
    """Persist and query project-level autopilot state."""

    def __init__(self, cwd: str | Path) -> None:
        self._cwd = Path(cwd).resolve()
        self._registry_path = get_project_autopilot_registry_path(self._cwd)
        self._journal_path = get_project_repo_journal_path(self._cwd)
        self._context_path = get_project_active_repo_context_path(self._cwd)
        self._runs_dir = get_project_autopilot_runs_dir(self._cwd)
        self._ensure_layout()

    def _run_command(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        shell: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            cwd=cwd or self._cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
            shell=shell,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""},
        )
        if check and completed.returncode != 0:
            output = (completed.stderr or completed.stdout).strip() or f"Command failed: {command}"
            raise RuntimeError(output)
        return completed

    def _run_git(self, args: list[str], *, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self._run_command(["git", *args], cwd=cwd, check=check)

    def _run_gh(self, args: list[str], *, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self._run_command(["gh", *args], cwd=cwd, check=check)

    def _gh_json(self, args: list[str], *, cwd: Path | None = None) -> Any:
        completed = self._run_gh(args, cwd=cwd, check=True)
        raw = (completed.stdout or "").strip()
        if not raw:
            return None
        return json.loads(raw)

    def _git_has_changes(self, cwd: Path) -> bool:
        completed = self._run_git(["status", "--porcelain"], cwd=cwd, check=True)
        return bool((completed.stdout or "").strip())

    def _is_git_repo(self, cwd: Path) -> bool:
        completed = self._run_git(["rev-parse", "--git-dir"], cwd=cwd)
        return completed.returncode == 0

    def _git_commit_all(self, cwd: Path, message: str) -> bool:
        if not self._git_has_changes(cwd):
            return False
        self._run_git(["add", "-A"], cwd=cwd, check=True)
        self._run_git(["commit", "-m", message], cwd=cwd, check=True)
        return True

    def _git_push_branch(self, cwd: Path, branch: str) -> None:
        self._run_git(["push", "-u", "origin", branch], cwd=cwd, check=True)

    def _git_branch_has_progress(self, cwd: Path, *, base_branch: str) -> bool:
        completed = self._run_git(
            ["rev-list", "--count", f"origin/{base_branch}..HEAD"],
            cwd=cwd,
        )
        if completed.returncode != 0:
            return False
        try:
            return int((completed.stdout or "0").strip() or "0") > 0
        except ValueError:
            return False

    def _sync_worktree_to_base(self, cwd: Path, *, base_branch: str, head_branch: str, reset: bool) -> None:
        self._run_git(["fetch", "origin", base_branch], cwd=cwd, check=True)
        if reset:
            self._run_git(["checkout", "-B", head_branch, f"origin/{base_branch}"], cwd=cwd, check=True)
            return
        self._run_git(["checkout", head_branch], cwd=cwd, check=True)

    def _run_gh_json(self, command: list[str]) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                command,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError("gh CLI is not installed.") from exc
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout).strip() or "gh command failed"
            raise ValueError(error)
        raw = (completed.stdout or "").strip()
        if not raw:
            return []
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("Expected gh JSON array output.")
        return [item for item in payload if isinstance(item, dict)]

    def _run_verification_steps(self, policies: dict[str, Any], *, cwd: Path | None = None) -> list[RepoVerificationStep]:
        steps: list[RepoVerificationStep] = []
        for cmd in self._verification_commands(policies):
            if cmd.error is not None:
                steps.append(
                    RepoVerificationStep(
                        command=cmd.raw,
                        returncode=-1,
                        status="error",
                        stderr=f"verification policy error: {cmd.error}",
                    )
                )
                continue
            target: str | list[str] = cmd.raw if cmd.shell else list(cmd.argv)
            try:
                completed = subprocess.run(
                    target,
                    cwd=cwd or self._cwd,
                    shell=cmd.shell,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=1800,
                )
                steps.append(
                    RepoVerificationStep(
                        command=cmd.raw,
                        returncode=completed.returncode,
                        status="success" if completed.returncode == 0 else "failed",
                        stdout=(completed.stdout or "")[-4000:],
                        stderr=(completed.stderr or "")[-4000:],
                    )
                )
            except FileNotFoundError as exc:
                steps.append(
                    RepoVerificationStep(
                        command=cmd.raw,
                        returncode=-1,
                        status="error",
                        stderr=f"executable not found: {exc}",
                    )
                )
            except subprocess.TimeoutExpired as exc:
                steps.append(
                    RepoVerificationStep(
                        command=cmd.raw,
                        returncode=-1,
                        status="error",
                        stdout=_safe_text(getattr(exc, "stdout", ""))[-4000:],
                        stderr=f"Timed out after {exc.timeout}s",
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                steps.append(
                    RepoVerificationStep(
                        command=cmd.raw,
                        returncode=-1,
                        status="error",
                        stderr=str(exc),
                    )
                )
        return steps
