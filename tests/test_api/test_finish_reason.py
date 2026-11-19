"""A max_tokens cutoff must be visible to the engine, not look like a finish."""

from __future__ import annotations

import json
from typing import Any

import pytest

try:  # Current SDK releases transport through httpx2.
    import httpx2 as httpx
except ImportError:  # pragma: no cover - older SDKs use httpx
    import httpx

from openharness.api.client import AnthropicApiClient, ApiMessageRequest
from openharness.api.finish_reason import FinishReason, is_truncated, normalize_finish_reason
from openharness.engine.messages import ConversationMessage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", FinishReason.END_TURN),
        ("stop", FinishReason.END_TURN),
        ("max_tokens", FinishReason.MAX_TOKENS),
        ("length", FinishReason.MAX_TOKENS),
        ("tool_use", FinishReason.TOOL_USE),
        ("tool_calls", FinishReason.TOOL_USE),
        ("refusal", FinishReason.REFUSAL),
        ("content_filter", FinishReason.CONTENT_FILTER),
        ("something_new", FinishReason.UNKNOWN),
        (None, FinishReason.UNKNOWN),
    ],
)
def test_finish_reason_normalization(raw: str | None, expected: FinishReason) -> None:
    assert normalize_finish_reason(raw) is expected


def test_only_max_tokens_counts_as_truncation() -> None:
    assert is_truncated(FinishReason.MAX_TOKENS) is True
    assert is_truncated(FinishReason.END_TURN) is False
    assert is_truncated(FinishReason.UNKNOWN) is False


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


def _anthropic_stream(*, stop_reason: str) -> bytes:
    return _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "half a sentence"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


async def _complete_event(stop_reason: str):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_anthropic_stream(stop_reason=stop_reason),
        )

    client = AnthropicApiClient(
        api_key="test-key",
        base_url="https://mock.local",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    request = ApiMessageRequest(
        model="claude-test",
        messages=[ConversationMessage.from_user_text("Say OK.")],
        max_tokens=8,
    )
    try:
        events = [event async for event in client.stream_message(request)]
    finally:
        await client.close()
    return next(e for e in events if type(e).__name__ == "ApiMessageCompleteEvent")


@pytest.mark.asyncio
async def test_adapter_reports_truncation_as_max_tokens() -> None:
    event = await _complete_event("max_tokens")
    assert event.finish_reason is FinishReason.MAX_TOKENS
    assert is_truncated(event.finish_reason)
    assert event.stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_adapter_reports_a_normal_finish() -> None:
    event = await _complete_event("end_turn")
    assert event.finish_reason is FinishReason.END_TURN
