"""Tests for the plain-text log written by headless agent workers."""

from __future__ import annotations

from io import StringIO

import pytest

from openharness.engine.messages import ConversationMessage, TextBlock, ThinkingBlock
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantThinkingDelta,
    AssistantTurnComplete,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.ui.app import _TaskWorkerLog


def _turn() -> AssistantTurnComplete:
    return AssistantTurnComplete(message=None, usage=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_worker_log_records_tool_activity_without_blank_line_runs():
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(AssistantTextDelta("I'll start by mapping the benchmark suite."))
    await log.render(_turn())
    for index in range(3):
        await log.render(
            ToolExecutionStarted(
                tool_name="Read",
                tool_input={"file_path": f"src/openharness/engine/part{index}.py"},
            )
        )
        await log.render(ToolExecutionCompleted(tool_name="Read", output="contents"))
        await log.render(_turn())
    await log.render(AssistantTextDelta("## Findings\nAll seams reviewed."))
    await log.render(_turn())

    text = stream.getvalue()
    assert "[tool 1] Read(file_path=src/openharness/engine/part0.py)" in text
    assert "[tool 3] Read(file_path=src/openharness/engine/part2.py)" in text
    assert "## Findings" in text
    # Tool-only turns must not emit one blank line each.
    assert "\n\n\n" not in text


@pytest.mark.asyncio
async def test_worker_log_streams_thinking_before_the_answer():
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(AssistantThinkingDelta("checking the seams. "))
    await log.render(AssistantThinkingDelta("now answer."))
    await log.render(AssistantTextDelta("the answer"))
    await log.render(
        AssistantTurnComplete(
            message=ConversationMessage(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="checking the seams. now answer.", signature="sig"),
                    TextBlock(text="the answer"),
                ],
            ),
            usage=None,  # type: ignore[arg-type]
        )
    )

    text = stream.getvalue()
    assert "[thinking] checking the seams. now answer." in text
    # Already streamed: must not be duplicated from the completed message.
    assert text.count("checking the seams") == 1
    assert "the answer" in text


@pytest.mark.asyncio
async def test_worker_log_falls_back_to_thinking_blocks():
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(
        AssistantTurnComplete(
            message=ConversationMessage(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="non-streamed plan", signature="sig"),
                    TextBlock(text="answer"),
                ],
            ),
            usage=None,  # type: ignore[arg-type]
        )
    )

    assert "[thinking] non-streamed plan" in stream.getvalue()
    assert "answer" in stream.getvalue()


@pytest.mark.asyncio
async def test_worker_log_falls_back_to_completed_message_text():
    """Non-streaming backends emit no text deltas; the final text must survive."""
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(
        AssistantTurnComplete(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="final answer without deltas")],
            ),
            usage=None,  # type: ignore[arg-type]
        )
    )

    assert stream.getvalue() == "final answer without deltas\n"


@pytest.mark.asyncio
async def test_worker_log_surfaces_tool_errors_and_status():
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(StatusEvent("retrying request"))
    await log.render(ToolExecutionStarted(tool_name="Bash", tool_input={"command": "false"}))
    await log.render(
        ToolExecutionCompleted(
            tool_name="Bash",
            output="command failed: permission denied\nsecond line",
            is_error=True,
        )
    )
    await log.render(ErrorEvent("turn aborted"))

    text = stream.getvalue()
    assert "[status] retrying request" in text
    assert "[tool error] Bash: command failed: permission denied" in text
    assert "second line" not in text
    assert "[error] turn aborted" in text


@pytest.mark.asyncio
async def test_worker_log_truncates_long_tool_arguments():
    stream = StringIO()
    log = _TaskWorkerLog(stream)

    await log.render(
        ToolExecutionStarted(
            tool_name="Write",
            tool_input={"file_path": "result.md", "content": "x" * 5000},
        )
    )

    tool_line = next(line for line in stream.getvalue().splitlines() if line.startswith("[tool 1]"))
    assert "file_path=result.md" in tool_line
    assert "x" * 5000 not in tool_line
    assert len(tool_line) < 260
