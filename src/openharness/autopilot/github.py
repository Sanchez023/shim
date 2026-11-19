"""GitHub intake and pull-request lifecycle operations."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from openharness.autopilot.base import AutopilotBase
from openharness.autopilot.helpers import (
    _bilingual_lines,
    _safe_text,
    _shorten,
    _source_ref_number,
)
from openharness.autopilot.types import RepoRunResult, RepoTaskCard


class GitHubMixin(AutopilotBase):
    """Issue/PR intake, comments, CI polling, and merge decisions."""

    def scan_github_issues(self, *, limit: int = 10) -> list[RepoTaskCard]:
        raw = self._run_gh_json(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,body,labels,updatedAt,url",
            ]
        )
        cards: list[RepoTaskCard] = []
        for item in raw:
            number = item.get("number")
            if number is None:
                continue
            labels = [str(label.get("name", "")).strip() for label in item.get("labels", [])]
            card, _ = self.enqueue_card(
                source_kind="github_issue",
                source_ref=f"issue:{number}",
                title=f"GitHub issue #{number}: {_safe_text(item.get('title'))}",
                body=_safe_text(item.get("body")),
                labels=[label for label in labels if label],
                metadata={
                    "url": _safe_text(item.get("url")),
                    "updated_at_remote": _safe_text(item.get("updatedAt")),
                },
            )
            cards.append(card)
        return cards

    def scan_github_prs(self, *, limit: int = 10) -> list[RepoTaskCard]:
        raw = self._run_gh_json(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,body,isDraft,reviewDecision,mergeStateStatus,updatedAt,url,labels,headRefName,baseRefName",
            ]
        )
        cards: list[RepoTaskCard] = []
        for item in raw:
            number = item.get("number")
            if number is None:
                continue
            labels = [str(label.get("name", "")).strip() for label in item.get("labels", [])]
            card, _ = self.enqueue_card(
                source_kind="github_pr",
                source_ref=f"pr:{number}",
                title=f"GitHub PR #{number}: {_safe_text(item.get('title'))}",
                body=_safe_text(item.get("body")),
                labels=[label for label in labels if label],
                metadata={
                    "url": _safe_text(item.get("url")),
                    "updated_at_remote": _safe_text(item.get("updatedAt")),
                    "is_draft": bool(item.get("isDraft")),
                    "review_decision": _safe_text(item.get("reviewDecision")),
                    "merge_state_status": _safe_text(item.get("mergeStateStatus")),
                    "head_ref_name": _safe_text(item.get("headRefName")),
                    "base_ref_name": _safe_text(item.get("baseRefName")),
                },
            )
            cards.append(card)
        return cards

    def scan_claude_code_candidates(
        self,
        *,
        limit: int = 10,
        root: str | Path | None = None,
    ) -> list[RepoTaskCard]:
        candidate_root = Path(root or Path.home() / "claude-code").expanduser().resolve()
        if not candidate_root.exists():
            raise ValueError(f"claude-code root not found: {candidate_root}")
        discovered: list[tuple[str, Path]] = []
        for dirname, label in (("commands", "command"), ("agents", "agent")):
            base = candidate_root / dirname
            if not base.exists():
                continue
            for path in sorted(base.iterdir(), key=lambda item: item.name.lower()):
                if path.name.startswith("."):
                    continue
                discovered.append((label, path))
        cards: list[RepoTaskCard] = []
        for label, path in discovered[:limit]:
            name = path.stem if path.is_file() else path.name
            card, _ = self.enqueue_card(
                source_kind="claude_code_candidate",
                source_ref=f"{label}:{path}",
                title=f"Evaluate claude-code {label}: {name}",
                body=(
                    f"Borrow candidate from {path}. "
                    "Review whether this should be aligned, adapted, or ignored for OpenHarness."
                ),
                metadata={"path": str(path)},
            )
            cards.append(card)
        return cards

    def scan_all_sources(self, *, issue_limit: int = 10, pr_limit: int = 10) -> dict[str, int]:
        counts = {"github_issue": 0, "github_pr": 0, "claude_code_candidate": 0}
        try:
            counts["github_issue"] = len(self.scan_github_issues(limit=issue_limit))
        except Exception as exc:
            self.append_journal(kind="scan_warning", summary=f"GitHub issue scan failed: {exc}")
        try:
            counts["github_pr"] = len(self.scan_github_prs(limit=pr_limit))
        except Exception as exc:
            self.append_journal(kind="scan_warning", summary=f"GitHub PR scan failed: {exc}")
        try:
            counts["claude_code_candidate"] = len(self.scan_claude_code_candidates(limit=8))
        except Exception as exc:
            self.append_journal(kind="scan_warning", summary=f"claude-code scan failed: {exc}")
        self.append_journal(kind="scan_all", summary=f"Scanned sources: {counts}")
        self.rebuild_active_context()
        return counts

    def _issue_number_for_card(self, card: RepoTaskCard) -> int | None:
        linked = card.metadata.get("linked_issue_numbers")
        if isinstance(linked, list) and linked:
            try:
                return int(linked[0])
            except (TypeError, ValueError):
                pass
        return _source_ref_number(card.source_ref, "issue")

    def _linked_pr_number(self, card: RepoTaskCard) -> int | None:
        linked = card.metadata.get("linked_pr_number")
        if linked is not None:
            try:
                return int(linked)
            except (TypeError, ValueError):
                return None
        return _source_ref_number(card.source_ref, "pr")

    def _current_repo_full_name(self) -> str:
        info = self._gh_json(["repo", "view", "--json", "nameWithOwner"], cwd=self._cwd) or {}
        repo = _safe_text(info.get("nameWithOwner"))
        if not repo:
            raise RuntimeError("Unable to resolve GitHub repository name with `gh repo view`.")
        return repo

    def _find_open_pr_for_branch(self, head_branch: str) -> dict[str, Any] | None:
        data = self._gh_json(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--head",
                head_branch,
                "--json",
                "number,url,isDraft,labels,headRefName,baseRefName,mergeStateStatus,reviewDecision",
            ],
            cwd=self._cwd,
        )
        if isinstance(data, list) and data:
            return cast(dict[str, Any], data[0])
        return None

    def _best_effort_add_labels(self, pr_number: int, labels: list[str]) -> None:
        normalized = [label for label in labels if label]
        if not normalized:
            return
        try:
            self._run_gh(["pr", "edit", str(pr_number), *sum([["--add-label", label] for label in normalized], [])], cwd=self._cwd)
        except Exception:
            self.append_journal(
                kind="github_warning",
                summary=f"Failed to add labels to PR #{pr_number}; continuing",
                metadata={"labels": normalized},
            )

    def _build_pr_body(
        self,
        card: RepoTaskCard,
        *,
        run_report_path: Path,
        verification_report_path: Path,
    ) -> str:
        issue_number = self._issue_number_for_card(card)
        body = [
            "## Autopilot Summary",
            "",
            f"- Task ID: `{card.id}`",
            f"- Source: `{card.source_kind}`",
            f"- Source ref: `{card.source_ref or '-'}`",
            "",
            "## Reports",
            "",
            f"- Run report: `{run_report_path}`",
            f"- Verification report: `{verification_report_path}`",
            "",
            "## Notes",
            "",
            "- Agent self-reported summary is not the source of truth.",
            "- Service-level local verification and remote CI status should be checked before merge.",
        ]
        if issue_number is not None:
            body.extend(["", f"Closes #{issue_number}"])
        return "\n".join(body).strip() + "\n"

    def _upsert_pull_request(
        self,
        card: RepoTaskCard,
        *,
        head_branch: str,
        base_branch: str,
        run_report_path: Path,
        verification_report_path: Path,
    ) -> dict[str, Any]:
        existing = self._find_open_pr_for_branch(head_branch)
        if existing is not None:
            existing_number = existing.get("number")
            if isinstance(existing_number, int):
                self._best_effort_add_labels(existing_number, ["autopilot"])
            return existing

        title = f"Autopilot: {card.title}"
        body = self._build_pr_body(
            card,
            run_report_path=run_report_path,
            verification_report_path=verification_report_path,
        )
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".md") as handle:
            handle.write(body)
            body_path = Path(handle.name)
        try:
            self._run_gh(
                [
                    "pr",
                    "create",
                    "--title",
                    title,
                    "--body-file",
                    str(body_path),
                    "--base",
                    base_branch,
                    "--head",
                    head_branch,
                ],
                cwd=self._cwd,
                check=True,
            )
        finally:
            body_path.unlink(missing_ok=True)

        created = self._find_open_pr_for_branch(head_branch)
        if created is None:
            raise RuntimeError(f"PR creation succeeded but PR for branch {head_branch} was not discoverable.")
        created_number = created.get("number")
        if isinstance(created_number, int):
            self._best_effort_add_labels(created_number, ["autopilot"])
        return created

    def _comment_on_issue(self, issue_number: int, comment: str) -> None:
        try:
            self._run_gh(["issue", "comment", str(issue_number), "--body", comment], cwd=self._cwd, check=True)
        except Exception as exc:
            self.append_journal(
                kind="github_warning",
                summary=f"Failed to comment on issue #{issue_number}: {exc}",
                metadata={"issue": issue_number},
            )

    def _comment_on_pr(self, pr_number: int, comment: str) -> None:
        try:
            self._run_gh(["pr", "comment", str(pr_number), "--body", comment], cwd=self._cwd, check=True)
        except Exception as exc:
            self.append_journal(
                kind="github_warning",
                summary=f"Failed to comment on PR #{pr_number}: {exc}",
                metadata={"pr": pr_number},
            )

    def _comment_started(self, card: RepoTaskCard, attempt_count: int) -> str:
        return _bilingual_lines(
            f"OpenHarness autopilot 已开始处理 `{card.id}`，当前第 {attempt_count} 轮执行。",
            f"OpenHarness autopilot started processing `{card.id}`. Attempt {attempt_count} is now running.",
        )

    def _comment_pr_opened(self, pr_number: int, pr_url: str) -> str:
        return _bilingual_lines(
            f"已创建或更新 PR #{pr_number}: {pr_url}",
            f"Created or updated PR #{pr_number}: {pr_url}",
        )

    def _comment_ci_failed(self, attempt_count: int, summary: str) -> str:
        return _bilingual_lines(
            f"远端 CI 失败，准备进入第 {attempt_count + 1} 轮自动修复。摘要：{summary}",
            f"Remote CI failed. Preparing repair round {attempt_count + 1}. Summary: {summary}",
        )

    def _comment_local_failed(self, attempt_count: int, summary: str) -> str:
        return _bilingual_lines(
            f"本地 verification 失败，准备进入第 {attempt_count + 1} 轮自动修复。摘要：{summary}",
            f"Local verification failed. Preparing repair round {attempt_count + 1}. Summary: {summary}",
        )

    def _comment_merged(self, pr_number: int) -> str:
        return _bilingual_lines(
            f"PR #{pr_number} 已自动合并，任务闭环完成。",
            f"PR #{pr_number} was auto-merged. The autopilot loop has completed.",
        )

    def _comment_human_gate(self, pr_number: int) -> str:
        return _bilingual_lines(
            f"PR #{pr_number} 的本地验证和远端 CI 都已通过，但仍需人工 gate 或 merge label。",
            f"PR #{pr_number} passed local verification and remote CI, but still requires a human gate or merge label.",
        )

    def _comment_terminal_failure(self, summary: str) -> str:
        return _bilingual_lines(
            f"自动化流程已停止。失败原因：{summary}",
            f"The automated loop has stopped. Failure reason: {summary}",
        )

    def _pr_status_snapshot(self, pr_number: int) -> dict[str, Any]:
        payload = self._gh_json(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,url,isDraft,labels,headRefName,baseRefName,mergeStateStatus,reviewDecision,statusCheckRollup",
            ],
            cwd=self._cwd,
        ) or {}
        payload["labels"] = [
            _safe_text(label.get("name"))
            for label in payload.get("labels", [])
            if isinstance(label, dict) and _safe_text(label.get("name"))
        ]
        return payload

    def _ci_rollup(self, pr_snapshot: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        checks = pr_snapshot.get("statusCheckRollup") or []
        normalized: list[dict[str, Any]] = []
        if not isinstance(checks, list):
            checks = []
        for item in checks:
            if not isinstance(item, dict):
                continue
            name = _safe_text(item.get("name") or item.get("context") or item.get("__typename") or "check")
            status = _safe_text(item.get("status")).upper()
            conclusion = _safe_text(item.get("conclusion")).upper()
            details_url = _safe_text(item.get("detailsUrl") or item.get("targetUrl"))
            normalized.append(
                {
                    "name": name,
                    "status": status,
                    "conclusion": conclusion,
                    "details_url": details_url,
                }
            )
        if not normalized:
            return "pending", "Remote CI checks have not appeared yet.", normalized
        if any(item["status"] in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"} or (not item["conclusion"] and item["status"] != "COMPLETED") for item in normalized):
            return "pending", "Remote CI is still running.", normalized
        failing = [
            item for item in normalized
            if item["conclusion"] and item["conclusion"] not in {"SUCCESS", "SKIPPED", "NEUTRAL"}
        ]
        if failing:
            summary = "; ".join(f"{item['name']}={item['conclusion']}" for item in failing[:4])
            return "failed", summary, normalized
        return "success", "All reported remote checks passed.", normalized

    async def _wait_for_pr_ci(self, pr_number: int, policies: dict[str, Any]) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
        github_policy = dict(policies.get("autopilot", {}).get("github", {}))
        timeout_seconds = int(github_policy.get("ci_timeout_seconds", 1800) or 1800)
        poll_interval = int(github_policy.get("ci_poll_interval_seconds", 20) or 20)
        no_checks_grace_seconds = int(github_policy.get("no_checks_grace_seconds", 60) or 60)
        checks_settle_seconds = int(github_policy.get("checks_settle_seconds", 20) or 20)
        deadline = time.time() + max(timeout_seconds, 30)
        no_checks_deadline = time.time() + max(no_checks_grace_seconds, poll_interval, 5)
        checks_seen_at: float | None = None
        while True:
            snapshot = self._pr_status_snapshot(pr_number)
            state, summary, checks = self._ci_rollup(snapshot)
            now = time.time()
            if checks and checks_seen_at is None:
                checks_seen_at = now
            if not checks and time.time() >= no_checks_deadline:
                return "success", "No remote checks were reported after the grace period.", snapshot, checks
            if state == "success" and checks and checks_seen_at is not None and now < checks_seen_at + max(checks_settle_seconds, 0):
                await asyncio.sleep(max(poll_interval, 5))
                continue
            if state in {"success", "failed"}:
                return state, summary, snapshot, checks
            if now >= deadline:
                return "failed", "Remote CI timed out.", snapshot, checks
            await asyncio.sleep(max(poll_interval, 5))

    def _automerge_eligible(self, pr_snapshot: dict[str, Any], policies: dict[str, Any]) -> bool:
        github_policy = dict(policies.get("autopilot", {}).get("github", {}))
        auto_merge = dict(github_policy.get("auto_merge", {}))
        mode = _safe_text(auto_merge.get("mode")) or "label_gated"
        required_label = _safe_text(auto_merge.get("required_label")) or "autopilot:merge"
        labels = {str(label).lower() for label in pr_snapshot.get("labels", [])}
        if bool(pr_snapshot.get("isDraft")):
            return False
        if mode == "pr_only":
            return False
        if mode == "fully_auto":
            return True
        return required_label.lower() in labels

    def _merge_pull_request(self, pr_number: int) -> None:
        self._run_gh(
            ["pr", "merge", str(pr_number), "--squash"],
            cwd=self._cwd,
            check=True,
        )

    def _prepare_repair_prompt(
        self,
        card: RepoTaskCard,
        policies: dict[str, Any],
        *,
        attempt_count: int,
        prior_summary: str | None,
        failure_stage: str | None,
        failure_summary: str | None,
    ) -> str:
        prompt = self._build_execution_prompt(card, policies)
        if attempt_count <= 1 or not failure_stage:
            return prompt
        extras = [
            "",
            "Repair context:",
            f"- Attempt: {attempt_count}",
            f"- Previous failure stage: {failure_stage}",
            f"- Previous failure summary: {failure_summary or '(none)'}",
        ]
        if prior_summary:
            extras.append(f"- Previous agent summary: {_shorten(prior_summary, limit=600)}")
        extras.extend(
            [
                "",
                "Repair instructions:",
                "- Make the smallest patch that fixes the reported failure.",
                "- Do not restart the task from scratch if the existing branch already contains valid progress.",
                "- Re-run the relevant verification commands after the fix.",
            ]
        )
        return prompt + "\n" + "\n".join(extras).strip() + "\n"

    async def _process_existing_pr_card(
        self,
        card: RepoTaskCard,
        pr_number: int,
        policies: dict[str, Any],
    ) -> RepoRunResult:
        current_run_report = self._runs_dir / f"{card.id}-run.md"
        current_verification_report = self._runs_dir / f"{card.id}-verification.md"
        self.update_status(
            card.id,
            status="waiting_ci",
            note=f"monitoring existing PR #{pr_number}",
            metadata_updates={"linked_pr_number": pr_number},
        )
        ci_state, ci_summary, pr_snapshot, _checks = await self._wait_for_pr_ci(pr_number, policies)
        pr_url = _safe_text(pr_snapshot.get("url"))
        if ci_state == "failed":
            self.update_status(
                card.id,
                status="failed",
                note=f"existing PR CI failed: {ci_summary}",
                metadata_updates={
                    "linked_pr_number": pr_number,
                    "linked_pr_url": pr_url,
                    "last_failure_stage": "remote_ci_failed",
                    "last_failure_summary": ci_summary,
                },
            )
            self._comment_on_pr(pr_number, self._comment_terminal_failure(ci_summary))
            return RepoRunResult(
                card_id=card.id,
                status="failed",
                run_report_path=str(current_run_report),
                verification_report_path=str(current_verification_report),
                pr_number=pr_number,
                pr_url=pr_url,
            )
        if self._automerge_eligible(pr_snapshot, policies):
            self._merge_pull_request(pr_number)
            self.update_status(
                card.id,
                status="merged",
                note=f"existing PR #{pr_number} merged automatically",
                metadata_updates={"linked_pr_number": pr_number, "linked_pr_url": pr_url},
            )
            self._comment_on_pr(pr_number, self._comment_merged(pr_number))
            return RepoRunResult(
                card_id=card.id,
                status="merged",
                run_report_path=str(current_run_report),
                verification_report_path=str(current_verification_report),
                pr_number=pr_number,
                pr_url=pr_url,
            )
        self.update_status(
            card.id,
            status="completed",
            note=f"existing PR #{pr_number} is green; human gate pending",
            metadata_updates={
                "linked_pr_number": pr_number,
                "linked_pr_url": pr_url,
                "human_gate_pending": True,
            },
        )
        self._comment_on_pr(pr_number, self._comment_human_gate(pr_number))
        return RepoRunResult(
            card_id=card.id,
            status="completed",
            run_report_path=str(current_run_report),
            verification_report_path=str(current_verification_report),
            pr_number=pr_number,
            pr_url=pr_url,
        )
