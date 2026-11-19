"""End-to-end tool profile activation through the query engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings, Settings
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.tools import create_default_tool_registry


class _ToolSearchFirstClient:
    def __init__(self) -> None:
        self.requests: list[ApiMessageRequest] = []

    async def stream_message(self, request: ApiMessageRequest):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="toolu_search",
                            name="tool_search",
                            input={"query": "image_generation"},
                        )
                    ],
                ),
                usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="done")],
            ),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
        )


def _tool_names(request: ApiMessageRequest) -> set[str]:
    return {str(tool["name"]) for tool in request.tools}


@pytest.mark.asyncio
async def test_tool_search_enables_deferred_tool_for_next_request(tmp_path: Path):
    client = _ToolSearchFirstClient()
    engine = QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(
            PermissionSettings(mode=PermissionMode.FULL_AUTO)
        ),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_turns=3,
        settings=Settings(tool_profile="core"),
    )

    events = [event async for event in engine.submit_message("find the image tool")]

    assert client.requests
    first = _tool_names(client.requests[0])
    assert "tool_search" in first
    assert "image_generation" not in first
    assert "bash" in first

    assert len(client.requests) >= 2
    second = _tool_names(client.requests[1])
    assert "image_generation" in second
    assert "tool_search" in second
    assert events
