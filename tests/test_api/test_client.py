from __future__ import annotations

from typing import ClassVar

import pytest

from openharness.api.client import (
    OAUTH_BETA_HEADER,
    AnthropicApiClient,
    ApiMessageRequest,
    ApiTextDeltaEvent,
    ApiThinkingDeltaEvent,
    _supports_anthropic_thinking,
    _thinking_budget_for_effort,
    build_anthropic_system_param,
)
from openharness.engine.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    assistant_message_from_api,
)


def test_anthropic_client_adds_oauth_beta_header(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)

    client = AnthropicApiClient(auth_token="oauth-token")
    _ = client._client  # the SDK client is built on first use

    assert captured["auth_token"] == "oauth-token"
    assert captured["default_headers"] == {"anthropic-beta": OAUTH_BETA_HEADER}


def test_anthropic_client_uses_api_key_without_oauth_beta(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)

    client = AnthropicApiClient(api_key="api-key")
    _ = client._client  # the SDK client is built on first use

    assert captured["api_key"] == "api-key"
    assert "default_headers" not in captured


def test_anthropic_client_adds_claude_oauth_identity_headers(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)
    monkeypatch.setattr(
        "openharness.auth.external.get_claude_code_version",
        lambda: "2.1.92",
    )
    monkeypatch.setattr(
        "openharness.auth.external.get_claude_code_session_id",
        lambda: "session-123",
    )
    monkeypatch.setattr(
        "openharness.api.client.get_claude_code_session_id",
        lambda: "session-123",
    )
    monkeypatch.setattr(
        "openharness.api.client.claude_attribution_header",
        lambda: "x-anthropic-billing-header: cc_version=2.1.92; cc_entrypoint=cli;",
    )

    client = AnthropicApiClient(auth_token="oauth-token", claude_oauth=True)
    _ = client._client  # the SDK client is built on first use

    headers = captured["default_headers"]
    assert captured["auth_token"] == "oauth-token"
    assert headers["x-app"] == "cli"
    assert headers["user-agent"] == "claude-cli/2.1.92 (external, cli)"
    assert headers["X-Claude-Code-Session-Id"] == "session-123"
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert "claude-code-20250219" in headers["anthropic-beta"]


def test_anthropic_thinking_model_detection_and_budgets():
    assert _supports_anthropic_thinking("claude-sonnet-4-6") is True
    assert _supports_anthropic_thinking("claude-opus-5-5") is True
    assert _supports_anthropic_thinking("claude-3-7-sonnet-20250219") is True
    assert _supports_anthropic_thinking("claude-3-5-sonnet-20241022") is False
    assert _supports_anthropic_thinking("gpt-5.5") is False
    assert _supports_anthropic_thinking("deepreasoning-ds-v4.1flash") is True

    assert _thinking_budget_for_effort("low") == 1024
    assert _thinking_budget_for_effort("medium") == 4096
    assert _thinking_budget_for_effort("high") == 8192
    assert _thinking_budget_for_effort("max") == 16384
    assert _thinking_budget_for_effort("off") is None


def test_anthropic_request_enables_thinking_from_effort():
    client = AnthropicApiClient(api_key="test-key")

    params = client._build_request_params(
        ApiMessageRequest(
            model="claude-sonnet-4-6",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="high",
        )
    )

    assert params["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert params["max_tokens"] == 8192 + 2048


def test_anthropic_request_omits_thinking_when_effort_is_off_or_model_is_old():
    client = AnthropicApiClient(api_key="test-key")

    off_params = client._build_request_params(
        ApiMessageRequest(
            model="claude-sonnet-4-6",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="off",
        )
    )
    assert "thinking" not in off_params

    old_model_params = client._build_request_params(
        ApiMessageRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="high",
        )
    )
    assert "thinking" not in old_model_params

    proxy_off_params = client._build_request_params(
        ApiMessageRequest(
            model="deepreasoning-ds-v4.1flash",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="off",
        )
    )
    assert proxy_off_params["thinking"] == {"type": "disabled"}

    proxy_on_params = client._build_request_params(
        ApiMessageRequest(
            model="deepreasoning-ds-v4.1flash",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="high",
        )
    )
    assert proxy_on_params["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_conversation_message_serializes_image_block_for_anthropic():
    message = ConversationMessage(
        role="user",
        content=[
            TextBlock(text="Describe this."),
            ImageBlock(media_type="image/png", data="YWJj", source_path="/tmp/example.png"),
        ],
    )

    assert message.to_api_param() == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this."},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "YWJj",
                },
            },
        ],
    }


def test_build_anthropic_system_param_adds_cache_control():
    prefix = "static prompt " * 200

    param = build_anthropic_system_param(
        f"{prefix}\n\ndynamic context",
        prefix,
    )

    assert isinstance(param, list)
    assert param[0]["text"] == prefix
    assert param[0]["cache_control"] == {"type": "ephemeral"}
    assert param[1] == {"type": "text", "text": "dynamic context"}


def test_build_anthropic_system_param_short_prefix_stays_string():
    assert build_anthropic_system_param("short prompt", "short") == "short prompt"


def test_build_anthropic_system_param_includes_attribution_in_cache_block():
    prefix = "static prompt " * 200

    param = build_anthropic_system_param(
        prefix,
        prefix,
        attribution="x-anthropic-billing-header: test",
    )

    assert isinstance(param, list)
    assert param[0]["text"].startswith("x-anthropic-billing-header: test\n")
    assert param[0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_assistant_message_preserves_thinking_blocks():
    class _Block:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Message:
        content: ClassVar[list[_Block]] = [
            _Block(type="thinking", thinking="reasoning", signature="sig"),
            _Block(type="redacted_thinking", data="opaque"),
            _Block(type="text", text="answer"),
        ]

    message = assistant_message_from_api(_Message())

    assert isinstance(message.content[0], ThinkingBlock)
    assert message.content[0].visibility == "session_private"
    assert message.to_api_param()["content"][0] == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "sig",
    }
    assert message.to_api_param()["content"][1] == {
        "type": "redacted_thinking",
        "data": "opaque",
    }


def test_anthropic_client_refreshes_claude_token_on_request(monkeypatch):
    captured_tokens: list[str] = []

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def __aiter__(self):
            if False:
                yield None

        async def get_final_message(self):
            class _Usage:
                input_tokens = 1
                output_tokens = 1

            class _Message:
                usage = _Usage()
                stop_reason = "end_turn"
                role = "assistant"
                content: ClassVar[list[object]] = []

            return _Message()

    class _FakeMessages:
        def __init__(self):
            self.last_params = None

        def stream(self, **params):
            self.last_params = params
            return _FakeStream()

    class _FakeBeta:
        def __init__(self):
            self.messages = _FakeMessages()

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured_tokens.append(kwargs["auth_token"])
            self.beta = _FakeBeta()
            self.messages = _FakeMessages()

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)
    monkeypatch.setattr(
        "openharness.auth.external.get_claude_code_session_id",
        lambda: "session-123",
    )
    monkeypatch.setattr(
        "openharness.api.client.get_claude_code_session_id",
        lambda: "session-123",
    )
    monkeypatch.setattr(
        "openharness.api.client.claude_attribution_header",
        lambda: "x-anthropic-billing-header: cc_version=2.1.92; cc_entrypoint=cli;",
    )

    current_token = {"value": "initial-token"}

    client = AnthropicApiClient(
        auth_token="initial-token",
        claude_oauth=True,
        auth_token_resolver=lambda: current_token["value"],
    )
    current_token["value"] = "refreshed-token"

    from openharness.api.client import ApiMessageRequest

    async def _run():
        events = []
        async for event in client.stream_message(
            ApiMessageRequest(
                model="claude-sonnet-4-6",
                messages=[],
                system_prompt="system prompt",
            )
        ):
            events.append(event)
        return events

    import asyncio

    events = asyncio.run(_run())

    # The SDK client is built lazily, so the first request already sees the
    # refreshed token (``initial-token`` never reaches the vendor SDK).
    assert captured_tokens == ["refreshed-token"]
    assert events
    assert client._client.beta.messages.last_params["metadata"] == {
        "user_id": '{"device_id":"openharness","session_id":"session-123","account_uuid":""}'
    }
    assert "oauth-2025-04-20" in client._client.beta.messages.last_params["betas"]
    assert client._client.beta.messages.last_params["system"].startswith(
        "x-anthropic-billing-header: cc_version=2.1.92; cc_entrypoint=cli;\n"
    )


def _message_cache_breakpoints(params: dict[str, object]) -> list[dict[str, object]]:
    messages = params["messages"]
    assert isinstance(messages, list)
    return [
        block
        for message in messages
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]


def test_anthropic_request_caches_the_conversation_tail(monkeypatch):
    monkeypatch.delenv("OPENHARNESS_PROMPT_CACHING", raising=False)
    client = AnthropicApiClient(api_key="test-key")
    messages = [ConversationMessage.from_user_text(f"turn {index}") for index in range(6)]

    params = client._build_request_params(
        ApiMessageRequest(model="claude-sonnet-4-6", messages=messages, effort="off")
    )

    marked = _message_cache_breakpoints(params)
    assert len(marked) == 4
    assert all(block["cache_control"] == {"type": "ephemeral"} for block in marked)
    assert params["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in params["messages"][0]["content"][0]


def test_system_cache_breakpoint_leaves_three_for_messages(monkeypatch):
    monkeypatch.delenv("OPENHARNESS_PROMPT_CACHING", raising=False)
    client = AnthropicApiClient(api_key="test-key")
    prefix = "stable system prompt\n" * 200

    params = client._build_request_params(
        ApiMessageRequest(
            model="claude-sonnet-4-6",
            messages=[ConversationMessage.from_user_text(f"turn {index}") for index in range(6)],
            system_prompt=prefix,
            system_prompt_cacheable_prefix=prefix,
            effort="off",
        )
    )

    assert isinstance(params["system"], list)
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert len(_message_cache_breakpoints(params)) == 3


def test_cache_breakpoints_skip_thinking_blocks(monkeypatch):
    monkeypatch.delenv("OPENHARNESS_PROMPT_CACHING", raising=False)
    client = AnthropicApiClient(api_key="test-key")
    message = ConversationMessage(
        role="assistant",
        content=[
            TextBlock(text="answer"),
            ThinkingBlock(thinking="private reasoning", signature="sig"),
        ],
    )

    params = client._build_request_params(
        ApiMessageRequest(model="claude-sonnet-4-6", messages=[message], effort="high")
    )

    blocks = params["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_prompt_caching_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENHARNESS_PROMPT_CACHING", "0")
    client = AnthropicApiClient(api_key="test-key")

    params = client._build_request_params(
        ApiMessageRequest(
            model="claude-sonnet-4-6",
            messages=[ConversationMessage.from_user_text("hi")],
            effort="off",
        )
    )

    assert _message_cache_breakpoints(params) == []


def test_anthropic_client_streams_thinking_deltas(monkeypatch):
    class _Delta:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Event:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Block:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def __aiter__(self):
            yield _Event(
                type="content_block_delta",
                delta=_Delta(type="thinking_delta", thinking="inspect the seams. "),
            )
            yield _Event(
                type="content_block_delta",
                delta=_Delta(type="thinking_delta", thinking="then answer."),
            )
            yield _Event(
                type="content_block_delta",
                delta=_Delta(type="text_delta", text="answer"),
            )

        async def get_final_message(self):
            class _Usage:
                input_tokens = 3
                output_tokens = 4

            class _Message:
                usage = _Usage()
                stop_reason = "end_turn"
                role = "assistant"

                def __init__(self):
                    self.content = [
                        _Block(
                            type="thinking",
                            thinking="inspect the seams. then answer.",
                            signature="sig",
                        ),
                        _Block(type="text", text="answer"),
                    ]

            return _Message()

    class _FakeMessages:
        def stream(self, **params):
            del params
            return _FakeStream()

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            del kwargs
            self.messages = _FakeMessages()

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)
    client = AnthropicApiClient(api_key="test-key")

    import asyncio

    async def _run():
        return [
            event
            async for event in client.stream_message(
                ApiMessageRequest(
                    model="claude-sonnet-4-6",
                    messages=[ConversationMessage.from_user_text("hi")],
                )
            )
        ]

    events = asyncio.run(_run())

    assert [
        event.text for event in events if isinstance(event, ApiThinkingDeltaEvent)
    ] == ["inspect the seams. ", "then answer."]
    assert [event.text for event in events if isinstance(event, ApiTextDeltaEvent)] == ["answer"]


@pytest.mark.asyncio
async def test_anthropic_warmup_sends_one_token_and_returns_usage(monkeypatch):
    captured: dict[str, object] = {}

    class _Usage:
        input_tokens = 5_800
        output_tokens = 1
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 1_024

    class _Message:
        usage = _Usage()

    class _Messages:
        async def create(self, **params):
            captured.update(params)
            return _Message()

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            del kwargs
            self.messages = _Messages()

    monkeypatch.setattr("openharness.api.client.AsyncAnthropic", _FakeAsyncAnthropic)
    client = AnthropicApiClient(api_key="test-key")
    system_prompt = "S" * 2_100

    usage = await client.warmup(
        ApiMessageRequest(
            model="deepseek-chat",
            messages=[],
            system_prompt=system_prompt,
            system_prompt_cacheable_prefix=system_prompt,
            max_tokens=1,
            tools=[
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
    )

    assert captured["max_tokens"] == 1
    assert captured["messages"] == [{"role": "user", "content": "warmup"}]
    assert "stream" not in captured
    assert captured["system"][0]["text"] == system_prompt
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["tools"][0]["name"] == "read_file"
    assert usage.input_tokens == 5_800
    assert usage.cache_read_input_tokens == 1_024
