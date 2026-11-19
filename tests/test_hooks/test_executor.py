"""Tests for hooks execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.hooks import HookEvent, HookExecutionContext, HookExecutor
from openharness.hooks.executor import _inject_arguments
from openharness.hooks.loader import HookRegistry
from openharness.hooks.schemas import CommandHookDefinition, PromptHookDefinition


class FakeApiClient:
    """Minimal fake streaming client."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream_message(self, request):
        del request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=self._text)]),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            stop_reason=None,
        )


@pytest.mark.asyncio
async def test_command_hook_executes(tmp_path: Path):
    registry = HookRegistry()
    registry.register(
        HookEvent.SESSION_START,
        CommandHookDefinition(command="printf 'booted'"),
    )
    executor = HookExecutor(
        registry,
        HookExecutionContext(cwd=tmp_path, api_client=FakeApiClient('{"ok": true}'), default_model="claude-test"),
    )

    result = await executor.execute(HookEvent.SESSION_START, {"event": "session_start"})

    assert result.blocked is False
    assert result.results[0].output == "booted"


@pytest.mark.asyncio
async def test_prompt_hook_can_block(tmp_path: Path):
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE,
        PromptHookDefinition(prompt="Check tool call", matcher="bash"),
    )
    executor = HookExecutor(
        registry,
        HookExecutionContext(
            cwd=tmp_path,
            api_client=FakeApiClient('{"ok": false, "reason": "blocked by policy"}'),
            default_model="claude-test",
        ),
    )

    result = await executor.execute(
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "bash", "tool_input": {"command": "rm -rf ."}},
    )

    assert result.blocked is True
    assert result.reason == "blocked by policy"


# ---------------------------------------------------------------------------
# _inject_arguments shell escaping
# ---------------------------------------------------------------------------


def test_inject_arguments_no_escape_by_default():
    payload = {"command": "$(whoami)"}
    result = _inject_arguments("echo $ARGUMENTS", payload)
    # Without shell_escape, the raw JSON is substituted
    assert result == 'echo {"command": "$(whoami)"}'


def test_inject_arguments_shell_escape_references_payload_env():
    payload = {"command": "$(whoami)"}
    result = _inject_arguments("echo $ARGUMENTS", payload, shell_escape=True)
    # With shell_escape the payload never enters the command text.  The shell
    # expands the env var itself, so no quoting dialect can re-parse it.
    assert result == 'echo "${OPENHARNESS_HOOK_PAYLOAD}"'
    assert "$(whoami)" not in result


def test_inject_arguments_shell_escape_matches_shell_family():
    payload = {"command": "x"}

    posix = _inject_arguments("hook $ARGUMENTS", payload, shell_escape=True, shell_family="posix")
    powershell = _inject_arguments(
        "hook $ARGUMENTS", payload, shell_escape=True, shell_family="powershell"
    )
    cmd = _inject_arguments("hook $ARGUMENTS", payload, shell_escape=True, shell_family="cmd")

    assert posix == 'hook "${OPENHARNESS_HOOK_PAYLOAD}"'
    assert powershell == "hook $env:OPENHARNESS_HOOK_PAYLOAD"
    assert cmd == "hook %OPENHARNESS_HOOK_PAYLOAD%"


@pytest.mark.asyncio
async def test_command_hook_escapes_shell_metacharacters(tmp_path: Path):
    """$ARGUMENTS in command hooks must be shell-escaped to prevent injection."""
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CommandHookDefinition(command="echo $ARGUMENTS"),
    )
    executor = HookExecutor(
        registry,
        HookExecutionContext(
            cwd=tmp_path,
            api_client=FakeApiClient('{"ok": true}'),
            default_model="claude-test",
        ),
    )

    # $(echo INJECTED) would execute as a subshell if not properly escaped
    payload = {"tool_name": "test", "input": "$(echo INJECTED)"}
    result = await executor.execute(HookEvent.PRE_TOOL_USE, payload)

    output = result.results[0].output
    # With proper escaping, the literal $(echo INJECTED) must survive.
    # Without escaping, bash expands the subshell and the $() wrapper is gone.
    assert "$(echo INJECTED)" in output
