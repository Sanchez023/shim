"""MCP image content must reach the model as image blocks, not base64 text.

A screenshot returned as ``{"type": "image", "data": "<base64>"}`` used to be
flattened by ``model_dump_json`` into the tool result text: it burned prompt
tokens on base64 the model cannot see.  These tests pin the new shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.step_runner import _take_tool_images
from openharness.engine.step_support import _preprocess_images_in_messages
from openharness.mcp.client import (
    MAX_IMAGE_B64_CHARS,
    MAX_IMAGES_PER_RESULT,
    McpClientManager,
    McpToolOutput,
)
from openharness.mcp.types import McpToolInfo
from openharness.tools.base import ToolExecutionContext
from openharness.tools.mcp_tool import McpToolAdapter

PNG_B64 = "iVBORw0KGgoAAAANSUhEUg=="


class _Item:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def model_dump_json(self) -> str:
        return '{"type": "blob"}'


class _Result:
    def __init__(self, content: list[_Item], structured: object | None = None) -> None:
        self.content = content
        self.structuredContent = structured


def _manager_returning(result: _Result) -> McpClientManager:
    manager = McpClientManager({})
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=result)
    manager._sessions["srv"] = session
    return manager


def _text(text: str) -> _Item:
    return _Item(type="text", text=text)


def _image(data: str = PNG_B64, mime_type: str = "image/png") -> _Item:
    return _Item(type="image", data=data, mimeType=mime_type)


@pytest.mark.asyncio
async def test_image_content_becomes_an_image_block() -> None:
    manager = _manager_returning(_Result([_text("Page title: Example"), _image()]))

    output = await manager.call_tool("srv", "browser_take_screenshot", {})

    assert isinstance(output, McpToolOutput)
    assert output.text == "Page title: Example"
    assert len(output.images) == 1
    assert output.images[0].media_type == "image/png"
    assert output.images[0].data == PNG_B64


@pytest.mark.asyncio
async def test_image_only_result_gets_a_placeholder_text() -> None:
    manager = _manager_returning(_Result([_image()]))

    output = await manager.call_tool("srv", "browser_take_screenshot", {})

    assert output.text == "[1 image(s) returned]"
    assert len(output.images) == 1


@pytest.mark.asyncio
async def test_text_only_result_is_unchanged() -> None:
    manager = _manager_returning(_Result([_text("line one"), _text("line two")]))

    output = await manager.call_tool("srv", "tool", {})

    assert output.text == "line one\nline two"
    assert output.images == []


@pytest.mark.asyncio
async def test_oversized_and_excess_images_are_omitted_with_a_note() -> None:
    oversized = _image(data="A" * (MAX_IMAGE_B64_CHARS + 1))
    many = [_image() for _ in range(MAX_IMAGES_PER_RESULT + 1)]
    manager = _manager_returning(_Result([oversized, *many]))

    output = await manager.call_tool("srv", "gallery", {})

    assert len(output.images) == MAX_IMAGES_PER_RESULT
    assert "2 image(s) omitted" in output.text


@pytest.mark.asyncio
async def test_non_image_mime_type_is_not_forwarded() -> None:
    manager = _manager_returning(_Result([_image(mime_type="application/pdf")]))

    output = await manager.call_tool("srv", "tool", {})

    assert output.images == []
    assert "1 image(s) omitted" in output.text


@pytest.mark.asyncio
async def test_adapter_forwards_images_on_the_tool_result() -> None:
    manager = _manager_returning(_Result([_text("shot"), _image()]))
    adapter = McpToolAdapter(
        manager,
        McpToolInfo(
            server_name="srv",
            name="browser_take_screenshot",
            description="screenshot",
            input_schema={"type": "object"},
        ),
    )

    result = await adapter.execute(
        adapter.input_model.model_validate({}),
        ToolExecutionContext(cwd=Path(".")),
    )

    assert result.output == "shot"
    assert [image.data for image in result.images] == [PNG_B64]


@pytest.mark.asyncio
async def test_adapter_still_accepts_a_text_only_manager() -> None:
    """Fakes and third-party managers that return plain text keep working."""

    class _LegacyManager:
        async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
            del server_name, tool_name, arguments
            return "plain text"

    adapter = McpToolAdapter(
        _LegacyManager(),  # type: ignore[arg-type]
        McpToolInfo(
            server_name="srv",
            name="tool",
            description="d",
            input_schema={"type": "object"},
        ),
    )

    result = await adapter.execute(
        adapter.input_model.model_validate({}),
        ToolExecutionContext(cwd=Path(".")),
    )

    assert result.output == "plain text"
    assert result.images == []


def test_tool_images_become_siblings_of_the_tool_results() -> None:
    tool_call = ToolUseBlock(id="toolu_1", name="mcp__playwright__browser_take_screenshot")
    result = ToolResultBlock(
        tool_use_id="toolu_1",
        content="[1 image(s) returned]",
        images=[ImageBlock(media_type="image/png", data=PNG_B64)],
    )

    blocks = _take_tool_images([tool_call], [result])

    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "[image from mcp__playwright__browser_take_screenshot]"
    assert isinstance(blocks[1], ImageBlock)
    assert blocks[1].origin_tool_use_id == "toolu_1"
    # The transport field is cleared so the payload is not carried twice.
    assert result.images == []


def test_tool_images_are_dumped_as_siblings_in_the_user_message() -> None:
    result = ToolResultBlock(tool_use_id="toolu_1", content="ok")
    message = ConversationMessage(
        role="user",
        content=[
            result,
            TextBlock(text="[image from tool]"),
            ImageBlock(media_type="image/png", data=PNG_B64, origin_tool_use_id="toolu_1"),
        ],
    )

    payload = message.to_api_param()["content"]

    assert [block["type"] for block in payload] == ["tool_result", "text", "image"]
    assert payload[2]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": PNG_B64,
    }
    # The transport-only field never reaches the wire.
    assert "images" not in payload[0]


@pytest.mark.asyncio
async def test_non_multimodal_model_still_converts_them_to_text() -> None:
    """The vision fallback keeps working for text-only models."""

    class _Tool:
        name = "image_to_text"

        class input_model:
            @staticmethod
            def model_validate(data: dict[str, object]) -> dict[str, object]:
                return data

        async def execute(self, parsed: object, context: object) -> object:
            del parsed, context

            class _R:
                is_error = False
                output = "a red and blue square"

            return _R()

    class _Registry:
        def get(self, name: str) -> object:
            return _Tool() if name == "image_to_text" else None

    class _Context:
        model = "gpt-3.5-turbo-instruct"
        tool_metadata: ClassVar[dict[str, object]] = {"vision_model_config": {"model": "vision-model"}}
        cwd = "."
        tool_registry = _Registry()

    messages = [
        ConversationMessage(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="toolu_1", content="shot"),
                ImageBlock(media_type="image/png", data=PNG_B64, origin_tool_use_id="toolu_1"),
            ],
        )
    ]

    events = [event async for event in _preprocess_images_in_messages(messages, _Context())]

    assert events, "the conversion status event should be emitted"
    assert isinstance(messages[0].content[1], TextBlock)
    assert messages[0].content[1].text == "a red and blue square"
