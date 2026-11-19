"""A truncated answer must reach the user as a warning, not as a finished turn."""

from __future__ import annotations

import pytest

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.finish_reason import FinishReason
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import StatusEvent, ToolExecutionCompleted
from openharness.permissions.checker import PermissionChecker
from openharness.permissions.modes import PermissionMode
from openharness.tools import create_default_tool_registry


class _TruncatingClient:
    def __init__(self, finish_reason: FinishReason) -> None:
        self._finish_reason = finish_reason

    async def stream_message(self, request):
        del request
        yield ApiTextDeltaEvent(text="def main():")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="def main():")],
            ),
            usage=UsageSnapshot(input_tokens=3, output_tokens=8),
            stop_reason=self._finish_reason.value,
            finish_reason=self._finish_reason,
        )


def _engine(tmp_path, finish_reason: FinishReason) -> QueryEngine:
    return QueryEngine(
        api_client=_TruncatingClient(finish_reason),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
        max_tokens=8,
    )


@pytest.mark.asyncio
async def test_max_tokens_emits_a_truncation_warning(tmp_path) -> None:
    events = [event async for event in _engine(tmp_path, FinishReason.MAX_TOKENS).submit_message("go")]

    warnings = [e for e in events if isinstance(e, StatusEvent) and "truncated" in e.message]
    assert warnings, "a max_tokens cutoff finished silently"
    assert "max_tokens" in warnings[0].message


@pytest.mark.asyncio
async def test_normal_finish_does_not_warn(tmp_path) -> None:
    events = [event async for event in _engine(tmp_path, FinishReason.END_TURN).submit_message("go")]

    assert not [e for e in events if isinstance(e, StatusEvent) and "truncated" in e.message]


@pytest.mark.asyncio
async def test_missing_completion_signal_is_reported(tmp_path) -> None:
    """ADR-0012 D2: a stream with no stop signal must not look like a clean finish."""
    events = [event async for event in _engine(tmp_path, FinishReason.UNKNOWN).submit_message("go")]

    warnings = [e for e in events if isinstance(e, StatusEvent) and "completion signal" in e.message]
    assert warnings, "an unprovable completion was presented as finished"


class _TruncatedToolCallClient:
    """Emits a ``write_file`` call cut off at max_tokens, then a normal answer.

    The arguments are valid JSON on purpose: that is the case where the old code
    happily parsed them and wrote half a file (ADR-0012 D1).
    """

    def __init__(self) -> None:
        self.calls = 0

    async def stream_message(self, request):
        del request
        self.calls += 1
        if self.calls == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="call_half",
                            name="write_file",
                            input={"path": "half.py", "content": "def main():"},
                        )
                    ],
                ),
                usage=UsageSnapshot(input_tokens=3, output_tokens=8),
                stop_reason="max_tokens",
                finish_reason=FinishReason.MAX_TOKENS,
            )
            return
        yield ApiTextDeltaEvent(text="done")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="done")],
            ),
            usage=UsageSnapshot(input_tokens=3, output_tokens=1),
            stop_reason="end_turn",
            finish_reason=FinishReason.END_TURN,
        )


@pytest.mark.asyncio
async def test_truncated_tool_call_is_never_executed(tmp_path) -> None:
    engine = QueryEngine(
        api_client=_TruncatedToolCallClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
        max_tokens=8,
    )

    events = [event async for event in engine.submit_message("go")]

    assert not (tmp_path / "half.py").exists(), "a truncated tool call wrote a partial file"
    results = [e for e in events if isinstance(e, ToolExecutionCompleted)]
    assert results, "the tool call must still get a result to keep pairing valid"
    assert all(result.is_error for result in results)
    assert "Not executed" in results[0].output
