"""Shared typing surface for the autopilot mixins."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from openharness.autopilot.helpers import _VerificationCommand
from openharness.autopilot.types import (
    RepoAutopilotRegistry,
    RepoJournalEntry,
    RepoRunResult,
    RepoTaskCard,
)


class AutopilotBase:
    """Attributes and cross-mixin capabilities shared by the store mixins.

    The mixins are combined into :class:`RepoAutopilotStore`; mypy cannot see
    sibling mixin members, so the shared surface is declared here.
    """

    _cwd: Path
    _registry_path: Path
    _journal_path: Path
    _context_path: Path
    _runs_dir: Path

    # Store API.
    append_journal: Callable[..., RepoJournalEntry]
    enqueue_card: Callable[..., tuple[RepoTaskCard, bool]]
    get_card: Callable[..., RepoTaskCard | None]
    list_cards: Callable[..., list[RepoTaskCard]]
    pick_next_card: Callable[..., RepoTaskCard | None]
    update_status: Callable[..., RepoTaskCard]
    load_journal: Callable[..., list[RepoJournalEntry]]
    load_active_context: Callable[..., str]
    rebuild_active_context: Callable[..., str]
    load_policies: Callable[..., dict[str, Any]]
    _load_registry: Callable[..., RepoAutopilotRegistry]
    export_dashboard: Callable[..., Path]

    # GitHub / pull-request capabilities.
    scan_all_sources: Callable[..., dict[str, int]]
    _gh_json: Callable[..., Any]
    _run_gh: Callable[..., subprocess.CompletedProcess[str]]
    _run_gh_json: Callable[..., list[dict[str, Any]]]
    _issue_number_for_card: Callable[..., int | None]
    _linked_pr_number: Callable[..., int | None]
    _prepare_repair_prompt: Callable[..., str]
    _process_existing_pr_card: Callable[..., Coroutine[Any, Any, RepoRunResult]]
    _upsert_pull_request: Callable[..., Any]
    _wait_for_pr_ci: Callable[
        ..., Coroutine[Any, Any, tuple[str, str, dict[str, Any], list[dict[str, Any]]]]
    ]
    _merge_pull_request: Callable[..., None]
    _automerge_eligible: Callable[..., bool]
    _comment_on_issue: Callable[..., None]
    _comment_on_pr: Callable[..., None]
    _comment_started: Callable[..., str]
    _comment_pr_opened: Callable[..., str]
    _comment_ci_failed: Callable[..., str]
    _comment_local_failed: Callable[..., str]
    _comment_merged: Callable[..., str]
    _comment_human_gate: Callable[..., str]
    _comment_terminal_failure: Callable[..., str]

    # Execution / verification capabilities.
    _build_execution_prompt: Callable[..., str]
    _verification_commands: Callable[..., list[_VerificationCommand]]
    _render_run_report: Callable[..., str]
    _render_verification_report: Callable[..., str]
    _run_verification_steps: Callable[..., list[Any]]

    # Git plumbing.
    _run_command: Callable[..., subprocess.CompletedProcess[str]]
    _run_git: Callable[..., subprocess.CompletedProcess[str]]
    _git_has_changes: Callable[..., bool]
    _is_git_repo: Callable[..., bool]
    _git_commit_all: Callable[..., bool]
    _git_push_branch: Callable[..., None]
    _git_branch_has_progress: Callable[..., bool]
    _sync_worktree_to_base: Callable[..., None]
