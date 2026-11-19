"""Behaviour of the verification ledger and the completion gate it feeds."""

from __future__ import annotations

import pytest

from openharness.engine import verification_ledger as ledger
from openharness.permissions.command_safety import is_read_only_command


def metadata(**options: object) -> dict[str, object]:
    """A fresh tool_metadata dict; options configure the gate."""

    return {ledger.CONFIG_KEY: {"enabled": True, **options}} if options else {}


def edit(metadata: dict[str, object], path: str = "src/openharness/engine/query.py") -> None:
    ledger.record_tool_call(
        metadata,
        tool_name="edit_file",
        tool_input={"file_path": path},
        is_error=False,
        resolved_file_path=path,
    )


def bash(
    metadata: dict[str, object],
    command: str,
    returncode: int = 0,
    **extra: object,
) -> None:
    ledger.record_tool_call(
        metadata,
        tool_name="bash",
        tool_input={"command": command},
        is_error=returncode != 0,
        resolved_file_path=None,
        result_metadata={"returncode": returncode, **extra},
        # Same classifier the dispatcher injects in production.
        read_only=is_read_only_command,
    )


def gate(metadata: dict[str, object], **kwargs: object) -> ledger.GateStatus:
    return ledger.status(metadata, nudge_limit=ledger.nudge_limit_from(metadata), **kwargs)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("uv run pytest -q", (ledger.KIND_TEST, ledger.SCOPE_FULL)),
        ("python -m pytest -q", (ledger.KIND_TEST, ledger.SCOPE_FULL)),
        ("pytest", (ledger.KIND_TEST, ledger.SCOPE_FULL)),
        ("pytest tests", (ledger.KIND_TEST, ledger.SCOPE_FULL)),
        ("pytest -k truncation", (ledger.KIND_TEST, ledger.SCOPE_PARTIAL)),
        ("pytest tests/test_api/test_errors.py", (ledger.KIND_TEST, ledger.SCOPE_PARTIAL)),
        ("pytest tests/test_api::test_x", (ledger.KIND_TEST, ledger.SCOPE_PARTIAL)),
        ("pytest --lf", (ledger.KIND_TEST, ledger.SCOPE_PARTIAL)),
        ("ruff check src tests scripts", (ledger.KIND_LINT, ledger.SCOPE_FULL)),
        ("ruff check src/openharness/api/client.py", (ledger.KIND_LINT, ledger.SCOPE_PARTIAL)),
        ("mypy src", (ledger.KIND_TYPECHECK, ledger.SCOPE_FULL)),
        ("npm run build", (ledger.KIND_BUILD, ledger.SCOPE_FULL)),
    ],
)
def test_classify_command(command: str, expected: tuple[str, str]) -> None:
    assert ledger.classify_command(command) == expected


def test_classify_ignores_the_interpreter_module_flag() -> None:
    # ``-m`` before ``pytest`` belongs to the interpreter, not a marker filter.
    assert ledger.classify_command("python -m pytest -q") == (
        ledger.KIND_TEST,
        ledger.SCOPE_FULL,
    )


def test_classify_ignores_the_cd_prefix_of_a_build() -> None:
    command = (
        "cd frontend/terminal && "
        "([ -x ./node_modules/.bin/tsc ] || npm ci --no-audit --no-fund) && "
        "./node_modules/.bin/tsc --noEmit"
    )
    assert ledger.classify_command(command) == (ledger.KIND_TYPECHECK, ledger.SCOPE_FULL)


def test_classify_returns_none_for_ordinary_commands() -> None:
    assert ledger.classify_command("ls -la") is None
    assert ledger.classify_command("git status") is None
    assert ledger.classify_command("ruff format src") is None


def test_gate_stays_closed_without_any_edit() -> None:
    data = metadata()
    bash(data, "pytest -q")
    assert ledger.status(data).needs_verification is False


def test_a_docs_only_edit_does_not_arm_the_gate() -> None:
    data = metadata()
    edit(data, "docs/adr/0014-completion-gate.md")
    assert ledger.status(data).needs_verification is False


def test_an_edit_with_an_unresolved_path_still_arms_the_gate() -> None:
    # The caller is an edit tool, so the write happened even if the path could
    # not be resolved; a missed arming would report it as verified silently.
    data = metadata()
    ledger.record_edit(data, None)
    assert ledger.status(data).needs_verification is True


def test_edit_then_green_full_run_closes_the_gate() -> None:
    data = metadata()
    edit(data)
    assert ledger.status(data).needs_verification is True
    bash(data, "pytest -q")
    assert ledger.status(data).needs_verification is False


def test_a_failed_full_run_does_not_close_the_gate() -> None:
    data = metadata()
    edit(data)
    bash(data, "pytest -q", returncode=1)
    status = ledger.status(data)
    assert status.needs_verification is True
    assert status.last_green is None


def test_a_partial_green_run_does_not_close_the_gate() -> None:
    data = metadata()
    edit(data)
    bash(data, "pytest tests/test_benchmarks")
    assert ledger.status(data).needs_verification is True


def test_editing_after_a_green_run_reopens_the_gate() -> None:
    data = metadata()
    edit(data)
    bash(data, "pytest -q")
    assert ledger.status(data).needs_verification is False
    edit(data)
    status = ledger.status(data)
    assert status.needs_verification is True
    assert "1 edit(s)" in status.reason


def test_git_bookkeeping_does_not_invalidate_a_green_run() -> None:
    data = metadata()
    edit(data)
    bash(data, "pytest -q")
    bash(data, "git add -A")
    bash(data, "git commit -m wip")
    assert ledger.status(data).needs_verification is False


def test_an_untracked_shell_write_invalidates_a_green_run() -> None:
    data = metadata()
    edit(data)
    bash(data, "pytest -q")
    bash(data, "sed -i 's/a/b/' src/openharness/engine/query.py")
    status = ledger.status(data)
    assert status.needs_verification is True
    assert "untracked shell writes" in status.reason


def test_a_fixing_lint_run_cannot_close_the_gate() -> None:
    data = metadata()
    edit(data)
    bash(data, "ruff check --fix src")
    assert ledger.status(data).needs_verification is True


def test_nudge_budget_is_spent_once_per_unverified_streak() -> None:
    data = metadata()
    edit(data)
    assert ledger.status(data, nudge_limit=1).nudge_allowed is True
    ledger.note_nudge(data)
    status = ledger.status(data, nudge_limit=1)
    assert status.needs_verification is True
    assert status.nudge_allowed is False


def test_nudge_budget_resets_after_a_green_run() -> None:
    data = metadata()
    edit(data)
    ledger.note_nudge(data)
    bash(data, "pytest -q")
    assert ledger.status(data, nudge_limit=1).nudge_allowed is True


def test_gate_can_be_disabled() -> None:
    data = metadata(enabled=False)
    edit(data)
    assert ledger.status(data).needs_verification is False


def test_tool_status_line_reports_staleness_and_goes_quiet_when_closed() -> None:
    data = metadata()
    edit(data)
    assert ledger.tool_status_line(data).startswith("[verification]")
    bash(data, "pytest -q")
    assert ledger.tool_status_line(data) == ""


def test_missing_metadata_is_a_no_op() -> None:
    ledger.record_tool_call(
        None,
        tool_name="edit_file",
        tool_input={"file_path": "src/x.py"},
        is_error=False,
        resolved_file_path="src/x.py",
    )
    assert ledger.status(None).needs_verification is False


def test_runs_are_capped_but_newest_survive() -> None:
    data = metadata()
    edit(data)
    for index in range(12):
        bash(data, f"pytest -q # {index}")
    recorded = ledger.runs(data)
    assert len(recorded) == ledger.MAX_TRACKED_RUNS
    assert recorded[-1].command.endswith("# 11")
