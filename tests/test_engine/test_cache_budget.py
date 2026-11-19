"""Cache-budget shrinking for large tool results."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.engine.cache_budget import (
    new_token_budget,
    shrink_tool_results_for_budget,
)
from openharness.engine.messages import ToolResultBlock


def test_new_token_budget_matches_the_95_percent_ceiling():
    assert new_token_budget(7_000) == 368
    assert new_token_budget(0) == 0
    # A tighter target leaves less room for new tokens.
    assert new_token_budget(7_000, 0.99) < new_token_budget(7_000, 0.90)


def test_large_tool_result_is_offloaded_to_fit_the_budget(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    result = ToolResultBlock(tool_use_id="tool-1", content="x" * 10_000)

    saved = shrink_tool_results_for_budget(
        [result],
        tool_names={"tool-1": "bash"},
        previous_prompt_tokens=7_000,
    )

    assert saved > 0
    assert result.content.startswith("[Tool output truncated]")
    assert "Full output saved to" in result.content
    artifacts = list((tmp_path / "data" / "tool_artifacts").glob("*.txt"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text(encoding="utf-8") == "x" * 10_000


def test_small_tool_result_is_left_alone(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    result = ToolResultBlock(tool_use_id="tool-2", content="short output")

    saved = shrink_tool_results_for_budget(
        [result],
        tool_names={"tool-2": "bash"},
        previous_prompt_tokens=7_000,
    )

    assert saved == 0
    assert result.content == "short output"


def test_budget_can_be_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENHARNESS_CACHE_BUDGET", "0")
    result = ToolResultBlock(tool_use_id="tool-3", content="x" * 10_000)

    saved = shrink_tool_results_for_budget(
        [result],
        tool_names={"tool-3": "bash"},
        previous_prompt_tokens=7_000,
    )

    assert saved == 0
    assert result.content == "x" * 10_000


def test_already_truncated_preview_is_shortened_without_reoffloading(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    original = (
        "[Tool output truncated]\n"
        "Tool: bash\n"
        "Original size: 9999 chars\n"
        "Full output saved to: /tmp/already.txt\n"
        "Inline preview: first 3000 chars\n\n"
        "Preview:\n" + "y" * 3_000
    )
    result = ToolResultBlock(tool_use_id="tool-4", content=original)

    saved = shrink_tool_results_for_budget(
        [result],
        tool_names={"tool-4": "bash"},
        previous_prompt_tokens=7_000,
    )

    assert saved > 0
    assert "preview shortened to fit the cache budget" in result.content
    assert "/tmp/already.txt" in result.content


@pytest.mark.asyncio
async def test_step_runner_shrinks_tool_results_before_the_follow_up_request(
    tmp_path: Path, monkeypatch
):
    from pydantic import BaseModel

    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.config.settings import PermissionSettings, Settings
    from openharness.engine.messages import (
        ConversationMessage,
        TextBlock,
        ToolUseBlock,
    )
    from openharness.engine.query_engine import QueryEngine
    from openharness.permissions import PermissionChecker
    from openharness.permissions.modes import PermissionMode
    from openharness.tools.base import BaseTool, ToolRegistry, ToolResult

    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    class _EmptyInput(BaseModel):
        pass

    class _BigTool(BaseTool):
        name = "big_tool"
        description = "Return a large output"
        input_model = _EmptyInput

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            return ToolResult(output="z" * 20_000)

    class _Client:
        def __init__(self) -> None:
            self.requests = []
            self._calls = 0

        async def stream_message(self, request):
            self.requests.append(request)
            self._calls += 1
            if self._calls == 1:
                message = ConversationMessage(
                    role="assistant",
                    content=[ToolUseBlock(id="tool-1", name="big_tool", input={})],
                )
            else:
                message = ConversationMessage(
                    role="assistant", content=[TextBlock(text="done")]
                )
            yield ApiMessageCompleteEvent(
                message=message,
                usage=UsageSnapshot(input_tokens=7_000, output_tokens=10),
                stop_reason="tool_use" if self._calls == 1 else "stop",
            )

    registry = ToolRegistry()
    registry.register(_BigTool())
    client = _Client()
    engine = QueryEngine(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="SYSTEM",
        settings=Settings(tool_profile="core"),
    )

    [event async for event in engine.submit_message("run the big tool")]

    assert len(client.requests) == 2
    follow_up = client.requests[1].messages
    tool_result = next(
        block
        for message in follow_up
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert tool_result.content.startswith("[Tool output truncated]")
    assert "preview shortened to fit the cache budget" in tool_result.content
    assert (tmp_path / "data" / "tool_artifacts").exists()
