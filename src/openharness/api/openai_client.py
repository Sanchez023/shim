"""OpenAI-compatible API client for providers like Alibaba DashScope, GitHub Models, etc."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openai import AsyncOpenAI

from openharness.api.capabilities import ModelCapabilities
from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    ApiTextDeltaEvent,
    ApiThinkingDeltaEvent,
)
from openharness.api.errors import (
    OpenHarnessApiError,
    RequestFailure,
    classify_api_error,
)
from openharness.api.finish_reason import normalize_finish_reason
from openharness.api.transport import RetryPolicy, stream_with_retry
from openharness.domain.messages import (
    ContentBlock,
    ConversationMessage,
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.domain.usage import UsageSnapshot

log = logging.getLogger(__name__)

# Importing ``openai`` (plus its ``types`` package) costs about half a second of
# interpreter time.  The terminal UI only shows its input box after the backend
# emits ``ready``, so the import is deferred until a request needs the SDK.
_OPENAI_MODULE: Any | None = None


def _openai_sdk() -> Any:
    """Return the imported ``openai`` module, importing it on first use."""
    global _OPENAI_MODULE
    if _OPENAI_MODULE is None:
        import openai

        _OPENAI_MODULE = openai
    return _OPENAI_MODULE

MAX_RETRIES = 3
# Multiplier applied to the computed backoff before sleeping; tests set 0.
_RETRY_DELAY_FACTOR = 1.0
_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
# Providers that reject ``stream_options`` on tool-using requests (or that
# enter a thinking mode requiring ``reasoning_content`` on every assistant
# message when it is present).  Everything else keeps ``include_usage`` so the
# harness can see prompt/cache accounting.
_STREAM_OPTIONS_UNSUPPORTED_WITH_TOOLS = ("kimi", "moonshot")
_DROP_STREAM_OPTIONS_ENV = "OPENHARNESS_DROP_STREAM_OPTIONS"


def _model_basename(model: str) -> str:
    """Return the model name without a provider prefix, lower-cased."""
    normalized = model.strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized


def _keep_stream_usage_with_tools(model: str) -> bool:
    """Return False for providers that cannot combine tools with usage chunks."""
    raw = os.environ.get(_DROP_STREAM_OPTIONS_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    return not _model_basename(model).startswith(_STREAM_OPTIONS_UNSUPPORTED_WITH_TOOLS)



def _normalize_effort(effort: str | None) -> str:
    normalized = (effort or "").strip().lower()
    if normalized in {"", "off", "none", "disabled", "disable"}:
        return "off"
    if normalized == "max":
        return "xhigh"
    return normalized


def _reasoning_effort_param(model: str, effort: str | None) -> dict[str, str]:
    """Map OH effort to OpenAI ``reasoning_effort`` for reasoning models."""
    normalized = _normalize_effort(effort)
    if normalized == "off":
        return {}
    normalized_model = model.strip().lower()
    if "/" in normalized_model:
        normalized_model = normalized_model.rsplit("/", 1)[-1]
    if not normalized_model.startswith(_REASONING_MODEL_PREFIXES):
        return {}
    mapped = "high" if normalized in {"high", "xhigh"} else normalized
    return {"reasoning_effort": mapped}


def _token_limit_param_for_model(model: str, max_tokens: int) -> dict[str, int]:
    """Return the correct token limit field for the target OpenAI model.

    GPT-5 and the current reasoning-model families reject ``max_tokens`` and
    require ``max_completion_tokens`` instead.
    """
    normalized = model.strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if normalized.startswith(_MAX_COMPLETION_TOKEN_MODEL_PREFIXES):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schemas to OpenAI function-calling format.

    Anthropic format:
        {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI format:
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result


def _convert_messages_to_openai(
    messages: list[ConversationMessage],
    system_prompt: str | None,
) -> list[dict[str, Any]]:
    """Convert Anthropic-style messages to OpenAI chat format.

    Key differences:
    - Anthropic: system prompt is a separate parameter
    - OpenAI: system prompt is a message with role="system"
    - Anthropic: tool_use / tool_result are content blocks
    - OpenAI: tool_calls on assistant message, tool results are separate messages
    """
    openai_messages: list[dict[str, Any]] = []

    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        if msg.role == "assistant":
            openai_msg = _convert_assistant_message(msg)
            openai_messages.append(openai_msg)
        elif msg.role == "user":
            # User messages may contain text or tool_result blocks
            tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
            user_blocks: list[ContentBlock] = [
                b for b in msg.content if isinstance(b, (TextBlock, ImageBlock))
            ]

            if tool_results:
                # Each tool result becomes a separate message with role="tool"
                for tr in tool_results:
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    })
            if user_blocks:
                content = _convert_user_content_to_openai(user_blocks)
                if isinstance(content, str):
                    if content.strip():
                        openai_messages.append({"role": "user", "content": content})
                elif content:
                    openai_messages.append({"role": "user", "content": content})
            if not tool_results and not user_blocks:
                # Empty user message (shouldn't happen, but handle gracefully)
                openai_messages.append({"role": "user", "content": ""})

    return openai_messages


def _convert_user_content_to_openai(blocks: list[ContentBlock]) -> str | list[dict[str, Any]]:
    """Convert user text/image blocks into OpenAI chat content."""
    has_image = any(isinstance(block, ImageBlock) for block in blocks)
    if not has_image:
        return "".join(block.text for block in blocks if isinstance(block, TextBlock))

    content: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{block.media_type};base64,{block.data}",
                },
            })
    return content


_EMPTY_REASONING_ENV = "OPENHARNESS_REQUIRE_EMPTY_REASONING_CONTENT"


def _empty_reasoning_required() -> bool:
    """True when the operator's provider requires an empty
    ``reasoning_content`` field on tool-using assistant messages
    (Kimi-on-Anthropic style). Default off — strict-OpenAI providers
    reject the field outright.
    """
    raw = os.environ.get(_EMPTY_REASONING_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _convert_assistant_message(msg: ConversationMessage) -> dict[str, Any]:
    """Convert an assistant ConversationMessage to OpenAI format.

    ``reasoning_content`` is a non-standard field used by thinking models
    (e.g. Kimi k2.5) to carry the model's internal chain-of-thought across
    turns. Some thinking-model providers require it on every assistant
    message with tool calls — even when empty — or they reject the request.
    Other OpenAI-compatible providers (Cerebras, OpenAI's own
    endpoint, etc.) reject the field outright with a 400
    ``wrong_api_format`` error.

    Behaviour:

    - When the streaming parser captured non-empty reasoning on
      ``msg._reasoning``, we always replay it. Models that emit reasoning
      tokens are by definition thinking models that round-trip them.
    - When there is no captured reasoning but the message has tool calls,
      we emit ``reasoning_content: ""`` only if the operator opts in via
      ``OPENHARNESS_REQUIRE_EMPTY_REASONING_CONTENT=1``. The default is
      omit, which matches strict-OpenAI providers.

    The opt-in default keeps strict-OpenAI providers (Cerebras, NVIDIA NIM,
    OpenAI direct, etc.) working out-of-the-box; Kimi-on-Anthropic users
    set the env var in their dotfiles or settings.
    """
    text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
    tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]

    openai_msg: dict[str, Any] = {"role": "assistant"}

    content = "".join(text_parts)
    openai_msg["content"] = content if content else None

    # Replay reasoning_content for thinking models.  ReasoningBlock is the
    # durable representation; the legacy ``_reasoning`` attribute remains as a
    # fallback for older session snapshots.
    reasoning_blocks = [
        block
        for block in msg.content
        if isinstance(block, ReasoningBlock)
        and block.provider in {"openai", ""}
        and block.text
    ]
    reasoning = "".join(block.text for block in reasoning_blocks) or getattr(
        msg, "_reasoning", None
    )
    if reasoning:
        openai_msg["reasoning_content"] = reasoning
    elif tool_uses and _empty_reasoning_required():
        # Kimi-style providers reject tool_use messages without this field
        # even when there's nothing to put in it. Opt-in via env var.
        openai_msg["reasoning_content"] = ""

    if tool_uses:
        openai_msg["tool_calls"] = [
            {
                "id": tu.id,
                "type": "function",
                "function": {
                    "name": tu.name,
                    "arguments": json.dumps(tu.input),
                },
            }
            for tu in tool_uses
        ]

    return openai_msg


def _parse_tool_arguments(name: str, raw: str) -> dict[str, Any]:
    """Parse streamed tool arguments, failing loudly on truncated JSON.

    A provider that cuts the JSON off mid-stream must not look like a valid
    no-argument call: silently substituting ``{}`` lets the harness execute a
    tool with the wrong inputs.
    """
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RequestFailure(
            f"Malformed tool arguments for {name!r}: {exc}; raw={raw[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RequestFailure(
            f"Malformed tool arguments for {name!r}: expected a JSON object, "
            f"got {type(parsed).__name__}"
        )
    return parsed


def _parse_assistant_response(response: Any) -> ConversationMessage:
    """Parse an OpenAI ChatCompletion response into a ConversationMessage."""
    choice = response.choices[0]
    message = choice.message
    content: list[ContentBlock] = []

    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning:
        content.append(ReasoningBlock(provider="openai", text=reasoning))

    if message.content:
        content.append(TextBlock(text=message.content))

    if message.tool_calls:
        for tc in message.tool_calls:
            content.append(ToolUseBlock(
                id=tc.id,
                name=tc.function.name,
                input=_parse_tool_arguments(tc.function.name, tc.function.arguments),
            ))

    return ConversationMessage(role="assistant", content=content)


def _usage_field(usage: Any, name: str) -> Any:
    """Read a usage field from an SDK object, its extras, or a raw dict."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(name)
    value = getattr(usage, name, None)
    if value is not None:
        return value
    extra = getattr(usage, "model_extra", None)
    if isinstance(extra, dict) and name in extra:
        return extra.get(name)
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            return data.get(name)
    return None


def _usage_data_from_chunk(usage: Any) -> dict[str, int]:
    """Split a provider prompt total into uncached input and cache reads.

    OpenAI reports ``prompt_tokens`` as the whole prompt and exposes the cached
    subset under ``prompt_tokens_details.cached_tokens``.  DeepSeek reports the
    same split as ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.
    Both are normalized to Anthropic's accounting so every provider sums the
    same way.
    """
    prompt_tokens = int(_usage_field(usage, "prompt_tokens") or 0)
    completion_tokens = int(_usage_field(usage, "completion_tokens") or 0)

    details = _usage_field(usage, "prompt_tokens_details")
    cached: Any = None
    if details is not None:
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
        else:
            cached = getattr(details, "cached_tokens", None)
            if cached is None:
                extra = getattr(details, "model_extra", None)
                if isinstance(extra, dict):
                    cached = extra.get("cached_tokens")
    if cached is None or int(cached or 0) <= 0:
        # DeepSeek reports the same value under its own field; prefer it when
        # ``prompt_tokens_details`` is absent or zero-filled.
        deepseek_hit = _usage_field(usage, "prompt_cache_hit_tokens")
        if deepseek_hit is not None:
            cached = deepseek_hit

    hit = int(cached or 0)
    miss = _usage_field(usage, "prompt_cache_miss_tokens")
    miss_int = int(miss) if miss is not None else None
    if prompt_tokens <= 0 and miss_int is not None:
        prompt_tokens = miss_int + hit
    if hit <= 0 and miss_int is not None and prompt_tokens > 0:
        # Some gateways only populate the miss counter.
        hit = max(prompt_tokens - miss_int, 0)

    hit = max(hit, 0)
    if prompt_tokens > 0:
        hit = min(hit, prompt_tokens)
    return {
        "input_tokens": max(prompt_tokens - hit, 0),
        "output_tokens": completion_tokens,
        "cache_read_input_tokens": hit,
    }


def _normalize_openai_base_url(base_url: str | None) -> str | None:
    """Normalize custom OpenAI-compatible base URLs without dropping API path segments."""
    if not base_url:
        return None
    trimmed = base_url.strip()
    if not trimmed:
        return None
    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return trimmed.rstrip("/")
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class OpenAICompatibleClient:
    """Client for OpenAI-compatible APIs (DashScope, GitHub Models, etc.).

    Implements the same SupportsStreamingMessages protocol as AnthropicApiClient
    so it can be used as a drop-in replacement in the agent loop.
    """

    # Vendor package this client imports lazily; the backend host warms it in
    # the background once the TUI is ready (see ``ReactBackendHost._warm_sdk``).
    sdk_module = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        # SDK-compatible async client (``httpx.AsyncClient``, or
        # ``httpx2.AsyncClient`` with newer SDK releases).  Injected by tests.
        http_client: Any | None = None,
        max_retries: int | None = None,
        retry_delay_factor: float | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "default_headers": {"Authorization": f"Bearer {api_key}"},
        }
        normalized_base_url = _normalize_openai_base_url(base_url)
        self.base_url = normalized_base_url
        if normalized_base_url:
            kwargs["base_url"] = normalized_base_url
        if timeout is not None:
            kwargs["timeout"] = timeout
        if http_client is not None:
            kwargs["http_client"] = http_client
        # This wrapper owns retries; the SDK must not retry underneath it.
        kwargs["max_retries"] = 0
        self._max_retries = MAX_RETRIES if max_retries is None else max(0, int(max_retries))
        self._retry_delay_factor = (
            _RETRY_DELAY_FACTOR
            if retry_delay_factor is None
            else max(0.0, float(retry_delay_factor))
        )
        # Learned per client instance after a provider rejects stream_options.
        self._drop_stream_options = False
        self._sdk_kwargs = kwargs
        # Built on first use: constructing the SDK client imports the vendor
        # package, which must not happen before the TUI reports itself ready.
        self.__sdk_client: AsyncOpenAI | None = None

    @property
    def _client(self) -> AsyncOpenAI:
        """Lazily created vendor SDK client."""
        if self.__sdk_client is None:
            sdk_class = globals().get("AsyncOpenAI") or _openai_sdk().AsyncOpenAI
            self.__sdk_client = sdk_class(**self._sdk_kwargs)
        return self.__sdk_client

    @_client.setter
    def _client(self, value: AsyncOpenAI) -> None:
        # Tests inject a stub SDK client directly.
        self.__sdk_client = value

    def capabilities(self) -> ModelCapabilities:
        """Declare OpenAI-compatible streaming, tools, vision and flat reasoning."""
        return ModelCapabilities(
            streaming=True,
            tools=True,
            images=True,
            reasoning=True,
            native_reasoning_state=False,
            implicit_prompt_cache=True,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        # Never build an SDK client just to close it.
        if self.__sdk_client is not None:
            await self.__sdk_client.close()

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Yield text deltas and the final message, matching the Anthropic client interface."""
        async for event in stream_with_retry(
            lambda: self._stream_once(request),
            policy=RetryPolicy(
                max_retries=self._max_retries,
                delay_factor=self._retry_delay_factor,
            ),
            translate_error=self._translate_error,
            on_retry=lambda notice: ApiRetryEvent(
                message=notice.message,
                attempt=notice.attempt,
                max_attempts=notice.max_attempts,
                delay_seconds=notice.delay_seconds,
            ),
            label="OpenAI",
        ):
            yield event

    async def warmup(self, request: ApiMessageRequest) -> UsageSnapshot:
        """Write the request prefix into the provider's implicit prompt cache.

        The call is deliberately non-streaming and asks for a single output
        token.  It is a best-effort latency/consistency measure: the provider
        bills the prefix once here, and the following real request can read it
        from cache.  Callers must treat failures as non-fatal.
        """
        params: dict[str, Any] = {
            "model": request.model,
            "messages": _convert_messages_to_openai(
                request.messages, request.system_prompt
            ),
            "stream": False,
        }
        if not _model_basename(request.model).startswith(_REASONING_MODEL_PREFIXES):
            # o-series/GPT-5 reject a non-default temperature.
            params["temperature"] = 0
        params.update(_token_limit_param_for_model(request.model, 1))
        if request.tools:
            params["tools"] = _convert_tools_to_openai(request.tools)
            params["parallel_tool_calls"] = True
        response = await self._client.chat.completions.create(**params)
        usage = getattr(response, "usage", None)
        data = _usage_data_from_chunk(usage) if usage is not None else {}
        return UsageSnapshot(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_input_tokens=data.get("cache_read_input_tokens", 0),
        )

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Single attempt: stream an OpenAI chat completion."""
        openai_messages = _convert_messages_to_openai(request.messages, request.system_prompt)
        openai_tools = _convert_tools_to_openai(request.tools) if request.tools else None

        params: dict[str, Any] = {
            "model": request.model,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        params.update(_token_limit_param_for_model(request.model, request.max_tokens))
        params.update(_reasoning_effort_param(request.model, request.effort))
        if openai_tools:
            params["tools"] = openai_tools
            params["parallel_tool_calls"] = True
            # Most providers return a final usage chunk so the harness can see
            # prompt/cache accounting.  Kimi/Moonshot reject (or change
            # thinking behavior on) stream_options for tool-using requests, so
            # only those models (and explicit opt-outs) drop the key.
            if (
                self._drop_stream_options
                or not _keep_stream_usage_with_tools(request.model)
            ):
                params.pop("stream_options", None)

        # Collect full response while streaming text deltas
        collected_content = ""
        collected_reasoning = ""
        collected_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_data: dict[str, int] = {}
        # Buffer to strip inline <think>…</think> blocks across streaming chunks.
        _think_buf = ""

        try:
            stream = await self._client.chat.completions.create(**params)
        except Exception as exc:
            # A few OpenAI-compatible gateways reject stream_options with a
            # 400 instead of ignoring it.  Retry once without it and remember
            # the decision for the rest of this client's lifetime.
            if "stream_options" not in str(exc).lower() or "stream_options" not in params:
                raise
            log.info("Provider rejected stream_options; retrying without usage chunks")
            self._drop_stream_options = True
            params.pop("stream_options", None)
            stream = await self._client.chat.completions.create(**params)

        async for chunk in stream:
            if not chunk.choices:
                # Usage-only chunk (some providers send this at the end)
                if chunk.usage:
                    usage_data = _usage_data_from_chunk(chunk.usage)
                continue

            delta = chunk.choices[0].delta
            chunk_finish = chunk.choices[0].finish_reason

            if chunk_finish:
                finish_reason = chunk_finish

            # Reasoning models emit either a dedicated reasoning_content field
            # or inline <think>…</think> blocks. Both are surfaced as thinking
            # deltas so UIs and task logs can show the reasoning process.
            reasoning_piece = getattr(delta, "reasoning_content", None) or ""
            if reasoning_piece:
                collected_reasoning += reasoning_piece
                yield ApiThinkingDeltaEvent(text=reasoning_piece)

            # Stream text content to user, stripping inline <think> blocks
            if delta.content:
                _think_buf += delta.content
                visible, _think_buf, thinking = _extract_think_blocks(_think_buf)
                if thinking:
                    collected_reasoning += thinking
                    yield ApiThinkingDeltaEvent(text=thinking)
                if visible:
                    collected_content += visible
                    yield ApiTextDeltaEvent(text=visible)

            # Accumulate tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = collected_tool_calls[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

            # Usage in chunk (if provider sends it)
            if chunk.usage:
                usage_data = _usage_data_from_chunk(chunk.usage)

        # Build the final ConversationMessage
        content: list[ContentBlock] = []
        if collected_content:
            content.append(TextBlock(text=collected_content))

        for _idx in sorted(collected_tool_calls.keys()):
            tc = collected_tool_calls[_idx]
            # Skip phantom/empty tool calls that some providers send
            if not tc["name"]:
                continue
            content.append(ToolUseBlock(
                id=tc["id"],
                name=tc["name"],
                input=_parse_tool_arguments(tc["name"], tc["arguments"]),
            ))

        if collected_reasoning:
            content.insert(0, ReasoningBlock(provider="openai", text=collected_reasoning))

        final_message = ConversationMessage(role="assistant", content=content)

        # Keep the legacy attribute for old snapshots and third-party clients.
        if collected_reasoning:
            final_message._reasoning = collected_reasoning  # type: ignore[attr-defined]

        yield ApiMessageCompleteEvent(
            message=final_message,
            usage=UsageSnapshot(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
            ),
            stop_reason=finish_reason,
            finish_reason=normalize_finish_reason(finish_reason),
        )

    @staticmethod
    def _translate_error(exc: Exception) -> OpenHarnessApiError:
        return classify_api_error(exc, type_name=exc.__class__.__name__)


# Matches complete <think>…</think> blocks (DOTALL so newlines are included).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_CAPTURE_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_OPEN_TAG = "<think>"


def _split_think_blocks(buf: str) -> tuple[str, str, str]:
    """Return ``(visible_text, leftover, thinking_text)`` for a stream buffer.

    Complete pairs are removed from the visible text and their contents are
    returned separately.  An unclosed ``<think>`` is held in *leftover* so it
    can be re-evaluated once the closing tag arrives in the next chunk.
    """
    thinking = "".join(match.group(1) for match in _THINK_CAPTURE_RE.finditer(buf))
    visible, leftover = _split_visible_text(buf)
    return visible, leftover, thinking


def _split_visible_text(buf: str) -> tuple[str, str]:
    """Strip complete ``<think>`` blocks and hold back an unclosed one."""
    cleaned = _THINK_RE.sub("", buf)

    open_idx = cleaned.find(_THINK_OPEN_TAG)
    if open_idx != -1:
        return cleaned[:open_idx], cleaned[open_idx:]

    # Streaming providers may split the opening tag itself across chunk
    # boundaries (e.g. ``"<thi"`` then ``"nk>..."``). Hold back the longest
    # suffix that could still become ``<think>`` on the next chunk.
    max_prefix = min(len(cleaned), len(_THINK_OPEN_TAG) - 1)
    for prefix_len in range(max_prefix, 0, -1):
        if _THINK_OPEN_TAG.startswith(cleaned[-prefix_len:]):
            return cleaned[:-prefix_len], cleaned[-prefix_len:]

    return cleaned, ""


def _strip_think_blocks(buf: str) -> tuple[str, str]:
    """Strip complete ``<think>…</think>`` blocks and return ``(visible_text, leftover)``."""
    visible, leftover, _thinking = _split_think_blocks(buf)
    return visible, leftover


def _extract_think_blocks(buf: str) -> tuple[str, str, str]:
    """Alias kept next to the streaming parser for clarity at the call site."""
    return _split_think_blocks(buf)


# ``AsyncOpenAI`` is resolved lazily so ``import openharness.api.openai_client``
# stays cheap.  Module-level ``__getattr__`` (PEP 562) keeps
# ``monkeypatch.setattr("openharness.api.openai_client.AsyncOpenAI", ...)`` working.
_LAZY_SDK_NAMES = frozenset({"AsyncOpenAI"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_SDK_NAMES:
        return getattr(_openai_sdk(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
