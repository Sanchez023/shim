"""Tests for the read-only bash command classifier."""

from pathlib import Path

import pytest

from openharness.config.settings import PermissionSettings
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.permissions.command_safety import (
    Verdict,
    classify_command,
    is_read_only_command,
    paths_in_command,
)
from openharness.tools.bash_tool import BashTool, BashToolInput

READ_ONLY_COMMANDS = [
    "git --version",
    "git status --short --branch",
    "git --version && git status --short --branch | head -20",
    "git log --oneline -5",
    "git diff --stat",
    "git diff --output-indicator-new='+' --stat",
    "git show HEAD",
    "git -C repo status",
    "git --no-pager log -1",
    "git branch -a",
    "git tag -l 'v*'",
    "git remote -v",
    "git config --get user.name",
    "git stash list",
    "git worktree list",
    "git reflog show",
    "git show-ref",
    "ls -la",
    "pwd",
    "cat README.md",
    "grep -rn TODO src",
    "rg needle src",
    "cd src && ls",
    "find src -name '*.py' -print",
    "wc -l README.md",
    "diff README.md README.zh-CN.md",
    "echo hello",
    "sort -u names.txt",
    "head -20 app.log",
    "git status 2>&1",
    "git status >/dev/null",
    "grep -rn TODO src 2>/dev/null",
    "git log --oneline 2>&1 | head -5",
    "cat README.md > /dev/null",
    "tree src",
    "jq . package.json",
    "du -sh src",
    "df -h",
    "fd -e py",
]

MUTATING_COMMANDS = [
    "rm -rf /",
    "git add .",
    "git commit -m 'x'",
    "git push origin main",
    "git checkout main",
    "git pull",
    "git branch new-feature",
    "git branch -D main",
    "git tag v1",
    "git tag -a v1 -m x",
    "git remote add origin https://example.com/repo.git",
    "git config user.name someone",
    "git config --global --unset user.name",
    "git stash",
    "git stash pop",
    "git reflog expire --all",
    "git worktree add /tmp/wt",
    "git -c alias.status=!rm-rf status",
    "git -c core.fsmonitor=/tmp/evil status",
    "git --git-dir=/tmp/repo status",
    "git --work-tree=/tmp status",
    "git diff --output=patch.diff",
    "git log --output=log.txt",
    "git status > out.txt",
    "echo hi >> log.txt",
    "git status 2>/dev/null > out.txt",
    "git status &> out.txt",
    "cat file <<EOF",
    "fd --exec rm",
    "fd -xrm",
    "tree -o out.txt",
    "tree -oout.txt",
    "true && rm x",
    "git status; rm x",
    "echo $(whoami)",
    "cat `whoami`",
    "cat $HOME/.ssh/id_rsa",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat ~/.openharness/credentials.json",
    "grep -r token ~",
    "grep -r token /",
    "grep -r token ~/.openharness",
    "find . -delete",
    "find . -exec rm {} +",
    "sort -o out.txt in.txt",
    "sort -oout.txt in.txt",
    "tail -f app.log",
    "sed -i 's/a/b/' f",
    "python -c 'print(1)'",
    "curl https://example.com",
    "xargs rm",
    "npm install",
    "cat file | tee copy.txt",
    "hostname new-name",
    "date -s 2020-01-01",
    "file -C -m magic",
    "rg --pre 'rm' foo",
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_commands_are_recognised(command: str) -> None:
    assert is_read_only_command(command) is True


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_mutating_or_unknown_commands_are_rejected(command: str) -> None:
    assert is_read_only_command(command) is False


def test_relative_paths_resolve_against_the_supplied_cwd(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    sensitive_dir = tmp_path / ".ssh"
    sensitive_dir.mkdir()
    (sensitive_dir / "id_rsa").write_text("key\n", encoding="utf-8")

    assert is_read_only_command("cat README.md", cwd=tmp_path) is True
    assert is_read_only_command("cat .ssh/id_rsa", cwd=tmp_path) is False
    assert is_read_only_command("ls", cwd=sensitive_dir) is False


def test_reported_regression_command_is_read_only() -> None:
    command = "git --version && git status --short --branch | head -20"

    assert BashTool().is_read_only(BashToolInput(command=command)) is True


def test_default_mode_auto_allows_read_only_bash() -> None:
    command = "git --version && git status --short --branch | head -20"
    tool = BashTool()
    arguments = BashToolInput(command=command)
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))

    decision = checker.evaluate(
        "bash",
        is_read_only=tool.is_read_only(arguments),
        command=command,
    )

    assert decision.allowed is True
    assert decision.requires_confirmation is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat /home/u/.ssh/id_rsa", ("/home/u/.ssh/id_rsa",)),
        ("head -n 2 notes.txt", ("2", "notes.txt")),
        # ``grep PATTERN file`` — PATTERN is a pattern, not a path (ADR-0013 D4).
        ("grep -r token .", (".",)),
        ("cat $HOME/.ssh/id_rsa", ("$HOME/.ssh/id_rsa",)),
        ("git --version && git status", ()),
    ],
)
def test_paths_in_command_recovers_path_like_tokens(command: str, expected: tuple[str, ...]) -> None:
    assert paths_in_command(command) == expected


def test_paths_in_command_keeps_read_command_arguments_without_a_separator() -> None:
    assert "notes.txt" in paths_in_command("cat notes.txt")


def test_default_mode_still_asks_for_mutating_bash() -> None:
    command = "mkdir -p scratch-dir"
    tool = BashTool()
    arguments = BashToolInput(command=command)
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))

    decision = checker.evaluate(
        "bash",
        is_read_only=tool.is_read_only(arguments),
        command=command,
    )

    assert decision.allowed is False
    assert decision.requires_confirmation is True


# --- ADR-0013: three-valued verdict -----------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # The false positive that motivated ADR-0013 D4: a backslash-escaped
        # ``\.ssh`` inside a grep pattern was normalised into a path separator
        # and matched ``*/.ssh/*``.
        ('grep -n "environ\\|shadow\\|\\.ssh\\|netrc" src', Verdict.READ_ONLY),
        ("ls -la", Verdict.READ_ONLY),
        ("cat .ssh/id_rsa", Verdict.MUTATING),
        ("find . -exec rm {} +", Verdict.MUTATING),
        ("rm -rf build", Verdict.MUTATING),
        ("git commit -m x", Verdict.MUTATING),
        # Not proven read-only, but not evidence of a write either.
        ("find . -name '*.py' | xargs wc -l", Verdict.UNKNOWN),
        ("wc -l $(find . -name '*.py')", Verdict.UNKNOWN),
        ("cat <<EOF\nhi\nEOF", Verdict.UNKNOWN),
    ],
)
def test_classify_command_separates_unknown_from_mutating(
    command: str, expected: Verdict
) -> None:
    assert classify_command(command).verdict is expected


def test_classify_command_reports_why_it_could_not_decide() -> None:
    verdict = classify_command("wc -l $(find . -name '*.py')")

    assert verdict.verdict is Verdict.UNKNOWN
    assert verdict.reason == "command_substitution"


def test_classify_command_reports_the_offending_write_flag() -> None:
    verdict = classify_command("find . -exec rm {} +")

    assert verdict.verdict is Verdict.MUTATING
    assert verdict.reason == "write_flag"
    assert verdict.trigger == "-exec"


def test_grep_pattern_is_never_treated_as_a_path() -> None:
    assert paths_in_command('grep -rn "\\.ssh" src') == ("src",)
