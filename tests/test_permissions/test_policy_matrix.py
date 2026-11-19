from __future__ import annotations

from pathlib import Path

import pytest

from openharness.config.settings import PermissionSettings
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.runtime.seams.governance.security import (
    CompositeSecurityPolicy,
    PermissionCheckerPolicy,
    SandboxPolicy,
    SecretAccessPolicy,
    SecurityDecision,
    SecurityRequest,
    build_default_security_policy,
)


class _StaticPolicy:
    def __init__(self, decision: SecurityDecision) -> None:
        self._decision = decision

    async def evaluate(self, request: SecurityRequest) -> SecurityDecision:
        del request
        return self._decision


def _request() -> SecurityRequest:
    return SecurityRequest(tool_name="read_file", is_read_only=True, file_path="/tmp/x")


@pytest.mark.asyncio
async def test_composite_policy_deny_wins_over_ask_and_allow():
    policy = CompositeSecurityPolicy(
        [
            _StaticPolicy(SecurityDecision(allowed=True, dimension="tool")),
            _StaticPolicy(
                SecurityDecision(
                    allowed=False,
                    requires_confirmation=True,
                    reason="ask",
                    dimension="approval",
                )
            ),
            _StaticPolicy(
                SecurityDecision(
                    allowed=False,
                    requires_confirmation=False,
                    reason="deny",
                    dimension="sandbox",
                )
            ),
        ]
    )

    decision = await policy.evaluate(_request())

    assert decision.allowed is False
    assert decision.requires_confirmation is False
    assert decision.reason == "deny"
    assert decision.dimension == "sandbox"


@pytest.mark.asyncio
async def test_composite_policy_ask_wins_over_allow():
    policy = CompositeSecurityPolicy(
        [
            _StaticPolicy(SecurityDecision(allowed=True, dimension="tool")),
            _StaticPolicy(
                SecurityDecision(
                    allowed=False,
                    requires_confirmation=True,
                    reason="ask",
                    dimension="approval",
                )
            ),
        ]
    )

    decision = await policy.evaluate(_request())

    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert decision.dimension == "approval"


@pytest.mark.asyncio
async def test_default_policy_preserves_full_auto_behaviour():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
    policy = build_default_security_policy(checker)

    decision = await policy.evaluate(_request())

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_permission_checker_policy_reports_tool_dimension():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))
    policy = PermissionCheckerPolicy(checker)

    decision = await policy.evaluate(_request())

    assert decision.dimension == "tool"
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_secret_access_policy_denies_ssh_material():
    policy = SecretAccessPolicy()

    decision = await policy.evaluate(
        SecurityRequest(
            tool_name="read_file",
            is_read_only=True,
            file_path=str(Path.home() / ".ssh" / "id_rsa"),
        )
    )

    assert decision.allowed is False
    assert decision.dimension == "secret"
    assert "sensitive credential path" in decision.reason


@pytest.mark.asyncio
async def test_secret_access_policy_denies_command_supplied_ssh_material():
    policy = SecretAccessPolicy()

    decision = await policy.evaluate(
        SecurityRequest(
            tool_name="bash",
            is_read_only=False,
            command="cat /home/u/.ssh/id_rsa",
            paths=("/home/u/.ssh/id_rsa",),
        )
    )

    assert decision.allowed is False
    assert decision.dimension == "secret"


@pytest.mark.asyncio
async def test_secret_access_policy_defers_command_paths_when_asked():
    policy = SecretAccessPolicy(defer_command_paths=lambda: True)

    decision = await policy.evaluate(
        SecurityRequest(
            tool_name="bash",
            is_read_only=False,
            command="cat /home/u/.ssh/id_rsa",
            paths=("/home/u/.ssh/id_rsa",),
        )
    )

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_secret_access_policy_allows_regular_files():
    policy = SecretAccessPolicy()

    decision = await policy.evaluate(
        SecurityRequest(
            tool_name="read_file",
            is_read_only=True,
            file_path="/tmp/project/main.py",
        )
    )

    assert decision.allowed is True
    assert decision.dimension == "secret"


@pytest.mark.asyncio
async def test_sandbox_policy_default_is_allow():
    policy = SandboxPolicy()

    decision = await policy.evaluate(_request())

    assert decision.allowed is True
    assert decision.dimension == "sandbox"
