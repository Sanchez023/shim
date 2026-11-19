"""Large user pastes are saved to disk instead of entering the prompt."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings, Settings
from openharness.engine.large_paste import offload_large_paste_message
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
)
from openharness.engine.query_engine import QueryEngine
from openharness.permissions import PermissionChecker
from openharness.tools import create_default_tool_registry


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    # Another test may leave coordinator mode enabled; it injects a synthetic
    # user context message that would become the last request message.
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


class _CapturingClient:
    def __init__(self) -> None:
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(dataclasses.replace(request, messages=list(request.messages)))
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text="ok")]),
            usage=UsageSnapshot(input_tokens=10, output_tokens=1),
            stop_reason="stop",
        )


def _engine(tmp_path: Path, client, **settings_overrides) -> QueryEngine:
    settings = Settings(
        api_key="test",
        model="deepseek-chat",
        permission=PermissionSettings(),
        **settings_overrides,
    )
    return QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="STABLE SYSTEM",
        system_prompt_cacheable_prefix="STABLE SYSTEM",
        settings=settings,
    )


def test_small_message_is_unchanged(tmp_path: Path):
    message = ConversationMessage.from_user_text("short prompt")

    offloaded, path = offload_large_paste_message(
        message, threshold_chars=100, preview_chars=10
    )

    assert offloaded is message
    assert path is None


def test_large_message_is_replaced_with_a_preview(tmp_path: Path):
    text = "HEAD " + ("middle " * 2_000) + "TAIL-MARKER"
    message = ConversationMessage.from_user_text(text)

    offloaded, path = offload_large_paste_message(
        message, threshold_chars=100, preview_chars=20
    )

    assert path is not None
    assert path.exists()
    assert path.read_text(encoding="utf-8") == text
    body = offloaded.text
    assert "[Large paste offloaded]" in body
    assert str(path) in body
    assert "HEAD" in body
    assert "TAIL-MARKER" in body
    assert "middle " * 100 not in body
    assert len(body) < len(text) / 4


def test_non_text_messages_are_not_offloaded(tmp_path: Path):
    message = ConversationMessage(
        role="user",
        content=[ToolResultBlock(tool_use_id="t", content="x" * 10_000)],
    )

    offloaded, path = offload_large_paste_message(
        message, threshold_chars=100, preview_chars=10
    )

    assert offloaded is message
    assert path is None


@pytest.mark.asyncio
async def test_engine_sends_the_preview_and_keeps_the_full_text(tmp_path: Path):
    client = _CapturingClient()
    engine = _engine(
        tmp_path,
        client,
        large_paste_threshold_chars=100,
        large_paste_preview_chars=20,
    )
    text = "HEAD " + ("payload " * 500) + "TAIL-MARKER"

    [event async for event in engine.submit_message(text)]

    sent = client.requests[0].messages[-1]
    assert "[Large paste offloaded]" in sent.text
    assert text not in sent.text
    artifact = Path(str(engine.tool_metadata["last_paste_artifact"]))
    assert artifact.read_text(encoding="utf-8") == text
    # The persisted history also carries only the preview, so later requests
    # reuse the same cached prefix instead of re-sending the paste.
    user_messages = [message for message in engine.messages if message.role == "user"]
    assert "[Large paste offloaded]" in user_messages[-1].text


@pytest.mark.asyncio
async def test_engine_can_disable_paste_offload(tmp_path: Path):
    client = _CapturingClient()
    engine = _engine(
        tmp_path,
        client,
        offload_large_pastes=False,
        large_paste_threshold_chars=100,
    )
    text = "payload " * 500

    [event async for event in engine.submit_message(text)]

    assert client.requests[0].messages[-1].text == text
    assert "last_paste_artifact" not in engine.tool_metadata
