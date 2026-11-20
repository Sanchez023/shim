"""Tests for permission decisions."""

import logging

import pytest

from openharness.config.settings import PathRuleConfig, PermissionSettings
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.permissions.checker import SENSITIVE_PATH_PATTERNS


def test_default_mode_allows_read_only():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    decision = checker.evaluate("read_file", is_read_only=True)
    assert decision.allowed is True
    assert decision.requires_confirmation is False


def test_default_mode_requires_confirmation_for_mutation():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    decision = checker.evaluate("write_file", is_read_only=False)
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert "/permissions full_auto" in decision.reason


def test_default_mode_gives_package_install_hint_for_bash():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    decision = checker.evaluate(
        "bash",
        is_read_only=False,
        command="npm init -y && npm install next react react-dom",
    )
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert "Package installation and scaffolding commands change the workspace" in decision.reason


def test_plan_mode_blocks_mutating_tools():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))
    decision = checker.evaluate("bash", is_read_only=False)
    assert decision.allowed is False
    assert "plan mode" in decision.reason


def test_plan_mode_asks_when_a_command_cannot_be_classified():
    """ADR-0013 D2: ``unknown`` is a request for confirmation, not a refusal.

    Refusing outright made ``wc -l $(find ...)`` and ``find | xargs`` dead ends
    in plan mode even though they only read.
    """

    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))
    decision = checker.evaluate(
        "bash",
        is_read_only=False,
        command="wc -l $(find . -name '*.py')",
        read_only_verdict="unknown",
    )

    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert "could not prove" in decision.reason


def test_plan_mode_still_refuses_provably_mutating_commands():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))
    decision = checker.evaluate(
        "bash",
        is_read_only=False,
        command="rm -rf build",
        read_only_verdict="mutating",
    )

    assert decision.allowed is False
    assert decision.requires_confirmation is False


def test_full_auto_allows_mutating_tools():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
    decision = checker.evaluate("bash", is_read_only=False)
    assert decision.allowed is True


# --- path_rules parsing tests ---


def _settings_with_rules(*rules) -> PermissionSettings:
    """Build a PermissionSettings with the given path_rule objects bypassing validation."""
    return PermissionSettings.model_construct(
        mode=PermissionMode.FULL_AUTO,
        allowed_tools=[],
        denied_tools=[],
        denied_commands=[],
        path_rules=list(rules),
    )


@pytest.mark.parametrize(
    "bad_rule",
    [
        PathRuleConfig.model_construct(allow=False),                  # pattern attribute missing
        PathRuleConfig.model_construct(pattern="", allow=False),      # pattern empty string
        PathRuleConfig.model_construct(pattern="   ", allow=False),   # pattern whitespace-only
        PathRuleConfig.model_construct(pattern=42, allow=False),      # pattern non-string
        PathRuleConfig.model_construct(pattern=None, allow=False),    # pattern None
    ],
    ids=["missing", "empty", "whitespace-only", "non-string", "none"],
)
def test_invalid_pattern_rule_is_skipped_and_warns(bad_rule, caplog):
    """Rules with missing, empty, or non-string patterns are skipped with a warning."""
    settings = _settings_with_rules(bad_rule)
    with caplog.at_level(logging.WARNING, logger="openharness.permissions.checker"):
        checker = PermissionChecker(settings)

    assert checker._path_rules == []
    assert "Skipping path rule" in caplog.text


def test_valid_deny_rule_blocks_matching_path():
    """A valid deny rule prevents access to a matching file path."""
    rule = PathRuleConfig(pattern="/etc/*", allow=False)
    settings = _settings_with_rules(rule)
    checker = PermissionChecker(settings)

    decision = checker.evaluate("read_file", is_read_only=True, file_path="/etc/passwd")
    assert decision.allowed is False
    assert "/etc/passwd" in decision.reason


def test_valid_deny_rule_does_not_block_non_matching_path():
    """A deny rule does not affect paths that don't match the pattern."""
    rule = PathRuleConfig(pattern="/etc/*", allow=False)
    settings = _settings_with_rules(rule)
    checker = PermissionChecker(settings)

    decision = checker.evaluate("read_file", is_read_only=True, file_path="/home/user/file.txt")
    assert decision.allowed is True


def test_valid_allow_rule_is_added():
    """A rule with allow=True is accepted and stored without warnings."""
    rule = PathRuleConfig(pattern="/data/*", allow=True)
    settings = _settings_with_rules(rule)
    checker = PermissionChecker(settings)

    assert len(checker._path_rules) == 1
    assert checker._path_rules[0].pattern == "/data/*"
    assert checker._path_rules[0].allow is True


def test_pattern_with_surrounding_whitespace_is_stripped():
    """A pattern with leading/trailing whitespace is accepted with whitespace stripped."""
    rule = PathRuleConfig.model_construct(pattern="  /etc/*  ", allow=False)
    settings = _settings_with_rules(rule)
    checker = PermissionChecker(settings)

    assert len(checker._path_rules) == 1
    assert checker._path_rules[0].pattern == "/etc/*"

    decision = checker.evaluate("read_file", is_read_only=True, file_path="/etc/passwd")
    assert decision.allowed is False


# --- built-in sensitive path protection tests ---


class TestSensitivePathProtection:
    """Built-in sensitive path patterns must block access in every permission mode."""

    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.FULL_AUTO, PermissionMode.DEFAULT, PermissionMode.PLAN],
        ids=["full_auto", "default", "plan"],
    )
    def test_ssh_key_blocked_in_all_modes(self, mode):
        checker = PermissionChecker(PermissionSettings(mode=mode))
        decision = checker.evaluate(
            "read_file", is_read_only=True, file_path="/home/user/.ssh/id_rsa"
        )
        assert decision.allowed is False
        assert ".ssh" in decision.reason

    def test_full_auto_blocks_sensitive_paths(self):
        """FULL_AUTO normally allows everything, but sensitive paths are still denied."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        for path in (
            "/home/user/.ssh/id_ed25519",
            "/home/user/.aws/credentials",
            "/home/user/.config/gcloud/application_default_credentials.json",
            "/home/user/.gnupg/private-keys-v1.d/key.key",
            "/home/user/.azure/accessTokens.json",
            "/home/user/.docker/config.json",
            "/home/user/.kube/config",
            "/home/user/.openharness/credentials.json",
            "/home/user/.openharness/copilot_auth.json",
        ):
            decision = checker.evaluate("read_file", is_read_only=True, file_path=path)
            assert decision.allowed is False, f"Expected {path} to be denied"

    def test_sensitive_path_blocks_write_tools(self):
        """Sensitive path protection applies to write operations too."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        decision = checker.evaluate(
            "write_file", is_read_only=False, file_path="/home/user/.ssh/authorized_keys"
        )
        assert decision.allowed is False

    def test_allowed_tools_does_not_bypass_sensitive_paths(self):
        """Even if read_file is explicitly allowed, sensitive paths are still denied."""
        checker = PermissionChecker(
            PermissionSettings(
                mode=PermissionMode.FULL_AUTO,
                allowed_tools=["read_file"],
            )
        )
        decision = checker.evaluate(
            "read_file", is_read_only=True, file_path="/home/user/.ssh/id_rsa"
        )
        assert decision.allowed is False

    def test_non_sensitive_paths_unaffected(self):
        """Normal project files are not blocked by sensitive path protection."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        for path in (
            "/home/user/project/src/main.py",
            "/home/user/.bashrc",
            "/home/user/.config/nvim/init.lua",
            "/tmp/scratch.txt",
        ):
            decision = checker.evaluate("read_file", is_read_only=True, file_path=path)
            assert decision.allowed is True, f"Expected {path} to be allowed"

    def test_harness_data_that_is_not_credentials_is_readable(self):
        """The whole ~/.openharness tree is not a credential store.

        Skills, plugins, themes and the harness's own tool artifacts are data
        the agent is expected to read.  Blocking them made ``ls
        ~/.openharness/skills`` and reading an offloaded tool artifact fail
        even though ``read_file`` could read the same file.
        """
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        for path in (
            "/home/user/.openharness/skills/harness-eval/SKILL.md",
            "/home/user/.openharness/plugins/tavily-search/plugin.json",
            "/home/user/.openharness/themes/cyberpunk.json",
            "/home/user/.openharness/data/tool_artifacts/20261120-132731-agent-abc.txt",
        ):
            decision = checker.evaluate("read_file", is_read_only=True, file_path=path)
            assert decision.allowed is True, f"Expected {path} to be allowed"
            decision = checker.evaluate(
                "bash",
                is_read_only=False,
                command=f"cat {path}",
                paths=(path,),
            )
            assert decision.allowed is True, f"Expected bash path {path} to be allowed"

    def test_narrowed_harness_sensitive_stores_still_blocked(self):
        """Session data, pastes, logs and settings backups stay protected."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        for path in (
            "/home/user/.openharness/data/sessions/proj/abc.events.jsonl",
            "/home/user/.openharness/sessions/legacy.json",
            "/home/user/.openharness/data/user_pastes/paste.txt",
            "/home/user/.openharness/logs/gateway.log",
            "/home/user/.openharness/settings.json.bak-20261119-164028",
        ):
            decision = checker.evaluate("read_file", is_read_only=True, file_path=path)
            assert decision.allowed is False, f"Expected {path} to be denied"
            decision = checker.evaluate(
                "bash",
                is_read_only=False,
                command=f"cat {path}",
                paths=(path,),
            )
            assert decision.allowed is False, f"Expected bash path {path} to be denied"

    def test_no_file_path_skips_sensitive_check(self):
        """Tools without a file path (e.g. bash) are not affected."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        decision = checker.evaluate("bash", is_read_only=False, command="echo hello")
        assert decision.allowed is True

    def test_full_auto_denies_command_supplied_sensitive_paths(self):
        """Bash has no file_path, so paths recovered from the command are checked."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        decision = checker.evaluate(
            "bash",
            is_read_only=False,
            command="cat /home/user/.ssh/id_rsa",
            paths=("/home/user/.ssh/id_rsa",),
        )
        assert decision.allowed is False
        assert decision.requires_confirmation is False

    def test_default_mode_asks_for_command_supplied_sensitive_paths(self):
        """Default mode keeps the existing confirmation prompt for the same command."""
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
        decision = checker.evaluate(
            "bash",
            is_read_only=False,
            command="cat /home/user/.ssh/id_rsa",
            paths=("/home/user/.ssh/id_rsa",),
        )
        assert decision.allowed is False
        assert decision.requires_confirmation is True

    def test_command_supplied_regular_paths_are_allowed_in_full_auto(self):
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        decision = checker.evaluate(
            "bash",
            is_read_only=False,
            command="cat /tmp/project/main.py",
            paths=("/tmp/project/main.py",),
        )
        assert decision.allowed is True

    @pytest.mark.parametrize(
        "pattern",
        SENSITIVE_PATH_PATTERNS,
        ids=[p.split("/")[-1] or p.split("/")[-2] for p in SENSITIVE_PATH_PATTERNS],
    )
    def test_every_builtin_pattern_has_coverage(self, pattern):
        """Verify every pattern in SENSITIVE_PATH_PATTERNS actually blocks something."""
        # Build a concrete path that should match the pattern
        example_paths = {
            "*/.ssh/*": "/home/u/.ssh/id_rsa",
            "*/.aws/credentials": "/home/u/.aws/credentials",
            "*/.aws/config": "/home/u/.aws/config",
            "*/.config/gcloud/*": "/home/u/.config/gcloud/creds.json",
            "*/.azure/*": "/home/u/.azure/tokens.json",
            "*/.gnupg/*": "/home/u/.gnupg/secring.gpg",
            "*/.docker/config.json": "/home/u/.docker/config.json",
            "*/.kube/config": "/home/u/.kube/config",
            "*/.openharness/credentials.json": "/home/u/.openharness/credentials.json",
            "*/.openharness/copilot_auth.json": "/home/u/.openharness/copilot_auth.json",
            "*/.openharness/settings.json*": "/home/u/.openharness/settings.json.bak-20261119",
            "*/.openharness/sessions/*": "/home/u/.openharness/sessions/abc.json",
            "*/.openharness/data/sessions/*": "/home/u/.openharness/data/sessions/p/abc.json",
            "*/.openharness/data/user_pastes/*": "/home/u/.openharness/data/user_pastes/p.txt",
            "*/.openharness/logs/*": "/home/u/.openharness/logs/gateway.log",
            "*/.netrc": "/home/u/.netrc",
            "*/.pypirc": "/home/u/.pypirc",
            "*/.npmrc": "/home/u/.npmrc",
            "*/.config/gh/*": "/home/u/.config/gh/hosts.yml",
        }
        test_path = example_paths[pattern]
        checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
        decision = checker.evaluate("read_file", is_read_only=True, file_path=test_path)
        assert decision.allowed is False, f"Pattern {pattern!r} did not block {test_path}"


def test_plan_mode_allows_todo_tracking_but_blocks_workspace_writes():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))

    tracking = checker.evaluate("todo_write", is_read_only=False)
    assert tracking.allowed is True

    blocked = checker.evaluate("write_file", is_read_only=False, file_path="app.py")
    assert blocked.allowed is False
    assert "plan mode" in blocked.reason


def test_plan_mode_requires_confirmation_to_switch_out():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))

    decision = checker.evaluate("exit_plan_mode", is_read_only=False)
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert "confirmation" in decision.reason

    blocked = checker.evaluate("write_file", is_read_only=False, file_path="app.py")
    assert blocked.allowed is False
    assert blocked.requires_confirmation is False
    assert "Shift+Tab" in blocked.reason


def test_plan_mode_allows_enter_plan_mode_but_not_nested_confirmation():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))

    decision = checker.evaluate("enter_plan_mode", is_read_only=False)

    assert decision.allowed is True
    assert decision.requires_confirmation is False


def test_plan_mode_allows_read_only_agent_delegation():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.PLAN))

    assert checker.evaluate("agent", is_read_only=True).allowed is True
    blocked = checker.evaluate("agent", is_read_only=False)
    assert blocked.allowed is False
    assert blocked.requires_confirmation is False
    assert "Shift+Tab" in blocked.reason


def test_write_outside_the_workspace_is_explained_not_crashing(tmp_path):
    """``_path_is_within`` used ``Path`` without importing it (NameError)."""
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    outside = str(tmp_path.parent / "elsewhere.py")

    decision = checker.evaluate(
        "write_file",
        is_read_only=False,
        file_path=outside,
        workspace_root=str(tmp_path),
    )

    assert decision.requires_confirmation is True
    assert "outside the workspace" in decision.reason
