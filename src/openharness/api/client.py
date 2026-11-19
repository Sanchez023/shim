"""Anthropic API client wrapper with retry logic."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import APIError, AsyncAnthropic

from openharness.api.capabilities import ModelCapabilities
from openharness.api.errors import (
    OpenHarnessApiError,
    RequestFailure,
    classify_api_error,
)
from openharness.api.finish_reason import FinishReason, normalize_finish_reason
from openharness.api.transport import RETRYABLE_STATUS_CODES, RetryPolicy, stream_with_retry
from openharness.auth.external import (
    claude_attribution_header,
    claude_oauth_betas,
    claude_oauth_headers,
    get_claude_code_session_id,
)
from openharness.domain.messages import ConversationMessage, assistant_message_from_api
from openharness.domain.usage import UsageSnapshot

log = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
# Multiplier applied to the computed backoff before sleeping.  Tests set this
# to 0 so they can assert the reported delay without waiting for it.
_RETRY_DELAY_FACTOR = 1.0
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Effort levels map to Anthropic extended-thinking budgets. ``off`` disables
# the thinking parameter entirely.
_EFFORT_ALIASES = {"max": "xhigh"}
_THINKING_BUDGETS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}

# Importing ``anthropic`` (together with its ``types`` package) costs roughly
# two seconds of interpreter time.  The TUI shows its input box only after the
# backend emits ``ready``, so that import is deferred to the first request that
# actually needs the vendor SDK.
_ANTHROPIC_MODULE: Any | None = None


def _anthropic_sdk() -> Any:
    """Return the imported ``anthropic`` module, importing it on first use."""
    global _ANTHROPIC_MODULE
    if _ANTHROPIC_MODULE is None:
        import anthropic

        _ANTHROPIC_MODULE = anthropic
    return _ANTHROPIC_MODULE


def _normalize_effort(effort: str | None) -> str:
    normalized = (effort or "").strip().lower()
    if normalized in {"", "off", "none", "disabled", "disable"}:
        return "off"
    return _EFFORT_ALIASES.get(normalized, normalized)


def _is_official_claude_model(model: str) -> bool:
    return model.strip().lower().rsplit("/", 1)[-1].startswith("claude")


def _supports_anthropic_thinking(model: str) -> bool:
    """Return True for models that accept Anthropic-style thinking.

    Besides Claude 3.7+/4+, Anthropic-compatible proxies often expose
    reasoning models under non-Claude names (for example
    ``deepreasoning-ds-v4.1flash``). Those endpoints accept the same
    ``thinking`` parameter, so recognize reasoning-flavoured model names too.
    """
    normalized = model.strip().lower().rsplit("/", 1)[-1]
    if normalized.startswith("claude"):
        # Claude 3.5 and older Claude 3 models do not support thinking.
        if re.search(r"claude-3(?:[.-]5|-opus|-haiku|-sonnet)", normalized):
            return False
        return bool(
            re.search(
                r"claude-3-7|claude-(?:sonnet|opus|haiku)-[4-9]|claude-[4-9]",
                normalized,
            )
        )
    return bool(
        re.search(
            r"reason|thinking|qwq|deepseek-r1|(?:^|[-_/])r1(?:$|[-_/])",
            normalized,
        )
    )


def _thinking_budget_for_effort(effort: str | None) -> int | None:
    normalized = _normalize_effort(effort)
    if normalized == "off":
        return None
    return _THINKING_BUDGETS.get(normalized)


@dataclass(frozen=True)
class ApiMessageRequest:
    """Input parameters for a model invocation."""

    model: str
    messages: list[ConversationMessage]
    system_prompt: str | None = None
    system_prompt_cacheable_prefix: str = ""
    max_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)
    effort: str | None = None


@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """Incremental text produced by the model."""

    text: str


@dataclass(frozen=True)
class ApiThinkingDeltaEvent:
    """Incremental model reasoning/thinking text.

    Kept separate from :class:`ApiTextDeltaEvent` so the answer stream stays
    clean while UIs can choose to display the reasoning that produced it.
    """

    text: str


@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """Terminal event containing the full assistant message."""

    message: ConversationMessage
    usage: UsageSnapshot
    stop_reason: str | None = None
    finish_reason: FinishReason = FinishReason.UNKNOWN


@dataclass(frozen=True)
class ApiRetryEvent:
    """A recoverable upstream failure that will be retried automatically."""

    message: str
    attempt: int
    max_attempts: int
    delay_seconds: float


ApiStreamEvent = (
    ApiTextDeltaEvent | ApiThinkingDeltaEvent | ApiMessageCompleteEvent | ApiRetryEvent
)


class SupportsStreamingMessages(Protocol):
    """Protocol used by the query engine in tests and production."""

    def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Return an async iterator of streamed events for the request."""


_MIN_CACHEABLE_SYSTEM_CHARS = 2_000

# Anthropic allows at most four cache breakpoints per request. The system
# prompt may already consume one, leaving the rest for the conversation tail.
_MAX_CACHE_BREAKPOINTS = 4
_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}
_CACHEABLE_BLOCK_TYPES = frozenset({"text", "image", "tool_use", "tool_result", "document"})


def prompt_caching_enabled() -> bool:
    """Return whether message-level prompt caching is enabled.

    Anthropic-compatible proxies do not all accept ``cache_control``; set
    ``OPENHARNESS_PROMPT_CACHING=0`` to opt out without patching code.
    """
    raw = os.environ.get("OPENHARNESS_PROMPT_CACHING", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _apply_message_cache_breakpoints(messages: list[dict[str, Any]], *, limit: int) -> int:
    """Mark the trailing messages with ephemeral cache breakpoints.

    Prompt caching is prefix based: a breakpoint at the end of message N caches
    everything up to N. Keeping breakpoints on the newest messages makes each
    turn reuse the previous turn's conversation prefix instead of billing the
    whole history as fresh input. Returns the number of breakpoints added.
    """
    if limit <= 0:
        return 0

    placed = 0
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") in _CACHEABLE_BLOCK_TYPES:
                block["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
                placed += 1
                break
        if placed >= limit:
            break
    return placed


def _system_cache_breakpoints(system_param: Any) -> int:
    """Count cache breakpoints already present on the system parameter."""
    if not isinstance(system_param, list):
        return 0
    return sum(
        1
        for block in system_param
        if isinstance(block, dict) and "cache_control" in block
    )


def build_anthropic_system_param(
    system_prompt: str | None,
    cacheable_prefix: str = "",
    *,
    attribution: str | None = None,
) -> str | list[dict[str, Any]] | None:
    """Build Anthropic's system parameter with an optional cache breakpoint."""
    text = system_prompt or ""
    prefix = cacheable_prefix or ""
    if attribution:
        text = f"{attribution}\n{text}" if text else attribution
        prefix = f"{attribution}\n{prefix}" if prefix else ""
    if not text:
        return None
    if (
        prefix
        and len(prefix) >= _MIN_CACHEABLE_SYSTEM_CHARS
        and text.startswith(prefix)
    ):
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        remainder = text[len(prefix) :].lstrip("\n")
        if remainder:
            blocks.append({"type": "text", "text": remainder})
        return blocks
    return text


class AnthropicApiClient:
    """Thin wrapper around the Anthropic async SDK with retry logic."""

    # Vendor package this client imports lazily; the backend host warms it in
    # the background once the TUI is ready (see ``ReactBackendHost._warm_sdk``).
    sdk_module = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        auth_token: str | None = None,
        base_url: str | None = None,
        claude_oauth: bool = False,
        auth_token_resolver: Callable[[], str] | None = None,
        enable_thinking: bool | None = None,
        # SDK-compatible async client (``httpx.AsyncClient``, or
        # ``httpx2.AsyncClient`` with newer SDK releases).  Injected by tests.
        http_client: Any | None = None,
        max_retries: int | None = None,
        retry_delay_factor: float | None = None,
    ) -> None:
        self._api_key = api_key
        self._auth_token = auth_token
        self._base_url = base_url
        self._claude_oauth = claude_oauth
        self._auth_token_resolver = auth_token_resolver
        self._http_client = http_client
        self._max_retries = MAX_RETRIES if max_retries is None else max(0, int(max_retries))
        self._retry_delay_factor = (
            _RETRY_DELAY_FACTOR
            if retry_delay_factor is None
            else max(0.0, float(retry_delay_factor))
        )
        # Subscription mode may auto-enable extended thinking; API-key mode
        # does not, so replaying thinking blocks there can be rejected.
        self._include_thinking = claude_oauth if enable_thinking is None else enable_thinking
        self._session_id = get_claude_code_session_id() if claude_oauth else ""
        # Created on first use: constructing the SDK client imports the vendor
        # package, which must not happen before the TUI reports itself ready.
        self.__sdk_client: AsyncAnthropic | None = None

    @property
    def _client(self) -> AsyncAnthropic:
        """Lazily created vendor SDK client."""
        if self.__sdk_client is None:
            self.__sdk_client = self._create_client()
        return self.__sdk_client

    @_client.setter
    def _client(self, value: AsyncAnthropic) -> None:
        # Tests (and OAuth token refresh) inject an SDK client directly.
        self.__sdk_client = value

    def _create_client(self) -> AsyncAnthropic:
        kwargs: dict[str, Any] = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._auth_token:
            kwargs["auth_token"] = self._auth_token
            kwargs["default_headers"] = (
                claude_oauth_headers()
                if self._claude_oauth
                else {"anthropic-beta": OAUTH_BETA_HEADER}
            )
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        # This wrapper owns retries; letting the SDK retry too would hide
        # ``Retry-After`` from the wrapper (and from ``ApiRetryEvent``).
        kwargs["max_retries"] = 0
        # ``AsyncAnthropic`` only exists as a module global when a caller (or a
        # test) patches it; otherwise resolve it from the lazily imported SDK.
        sdk_class = globals().get("AsyncAnthropic") or _anthropic_sdk().AsyncAnthropic
        client: AsyncAnthropic = sdk_class(**kwargs)
        return client

    def capabilities(self) -> ModelCapabilities:
        """Declare Anthropic streaming, tool, vision and thinking support."""
        return ModelCapabilities(
            streaming=True,
            tools=True,
            images=True,
            reasoning=True,
            native_reasoning_state=True,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        # Never build an SDK client just to close it.
        if self.__sdk_client is not None:
            await self.__sdk_client.close()

    def _refresh_client_auth(self) -> None:
        if not self._claude_oauth or self._auth_token_resolver is None:
            return
        next_token = self._auth_token_resolver()
        if next_token and next_token != self._auth_token:
            self._auth_token = next_token
            self._client = self._create_client()

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Yield text deltas and the final assistant message with retry on transient errors."""
        policy = RetryPolicy(
            max_retries=self._max_retries,
            delay_factor=self._retry_delay_factor,
        )

        def _open() -> AsyncIterator[ApiStreamEvent]:
            self._refresh_client_auth()
            return self._stream_once(request)

        def _translate(exc: Exception) -> Exception:
            if isinstance(exc, OpenHarnessApiError):
                return exc
            if isinstance(exc, _anthropic_sdk().APIError):
                return _translate_api_error(exc)
            return RequestFailure(str(exc))

        async for event in stream_with_retry(
            _open,
            policy=policy,
            translate_error=_translate,
            on_retry=lambda notice: ApiRetryEvent(
                message=notice.message,
                attempt=notice.attempt,
                max_attempts=notice.max_attempts,
                delay_seconds=notice.delay_seconds,
            ),
            label="Anthropic",
        ):
            yield event

    def _build_request_params(self, request: ApiMessageRequest) -> dict[str, Any]:
        """Build the Anthropic Messages API parameters for one request."""
        thinking_capable = _supports_anthropic_thinking(request.model)
        budget = _thinking_budget_for_effort(request.effort) if thinking_capable else None
        thinking_disabled = (
            thinking_capable
            and budget is None
            and not _is_official_claude_model(request.model)
        )
        include_thinking = self._include_thinking or budget is not None
        message_params = [
            message.to_api_param(include_thinking=include_thinking)
            for message in request.messages
        ]
        params: dict[str, Any] = {
            "model": request.model,
            "messages": message_params,
            "max_tokens": request.max_tokens,
        }
        if budget is not None:
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic requires max_tokens to exceed the thinking budget.
            params["max_tokens"] = max(int(request.max_tokens), budget + 2048)
        elif thinking_disabled:
            # Custom Anthropic-compatible endpoints may default reasoning to
            # on; an explicit disabled marker turns it off for /effort off.
            params["thinking"] = {"type": "disabled"}
        system_param = build_anthropic_system_param(
            request.system_prompt,
            request.system_prompt_cacheable_prefix,
            attribution=claude_attribution_header() if self._claude_oauth else None,
        )
        if system_param is not None:
            params["system"] = system_param
        if prompt_caching_enabled():
            _apply_message_cache_breakpoints(
                message_params,
                limit=max(0, _MAX_CACHE_BREAKPOINTS - _system_cache_breakpoints(system_param)),
            )
        if request.tools:
            params["tools"] = request.tools
        if self._claude_oauth:
            params["betas"] = claude_oauth_betas()
            params["metadata"] = {
                "user_id": json.dumps(
                    {
                        "device_id": "openharness",
                        "session_id": self._session_id,
                        "account_uuid": "",
                    },
                    separators=(",", ":"),
                )
            }
            params["extra_headers"] = {"x-client-request-id": str(uuid.uuid4())}
        return params

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Single attempt at streaming a message."""
        params = self._build_request_params(request)

        try:
            stream_api = self._client.beta.messages if self._claude_oauth else self._client.messages
            async with stream_api.stream(**params) as stream:
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            yield ApiThinkingDeltaEvent(text=thinking)
                        continue
                    if delta_type != "text_delta":
                        continue
                    text = getattr(delta, "text", "")
                    if text:
                        yield ApiTextDeltaEvent(text=text)

                final_message = await stream.get_final_message()
        except _anthropic_sdk().APIError as exc:
            if (
                isinstance(exc, _anthropic_sdk().APIStatusError)
                and exc.status_code in RETRYABLE_STATUS_CODES
            ):
                raise  # Let retry logic handle it
            raise _translate_api_error(exc) from exc

        usage = getattr(final_message, "usage", None)
        raw_stop_reason = getattr(final_message, "stop_reason", None)
        yield ApiMessageCompleteEvent(
            message=assistant_message_from_api(final_message),
            usage=UsageSnapshot(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                ),
            ),
            stop_reason=raw_stop_reason,
            finish_reason=normalize_finish_reason(raw_stop_reason),
        )

    async def warmup(self, request: ApiMessageRequest) -> UsageSnapshot:
        """Write the request prefix into the provider's prompt cache.

        Anthropic-compatible endpoints (including DeepSeek's
        ``/anthropic`` gateway) still perform automatic prefix caching, but
        the Messages API requires at least one message.  A one-token dummy
        user message sits *after* the system+tools prefix, so the common
        prefix is still cached and the real request only misses its own user
        message.
        """
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "warmup"}],
        }
        system_param = build_anthropic_system_param(
            request.system_prompt,
            request.system_prompt_cacheable_prefix,
            attribution=claude_attribution_header() if self._claude_oauth else None,
        )
        if system_param is not None:
            params["system"] = system_param
        if request.tools:
            params["tools"] = request.tools
        messages_api = (
            self._client.beta.messages if self._claude_oauth else self._client.messages
        )
        response = await messages_api.create(**params)
        usage = getattr(response, "usage", None)
        return UsageSnapshot(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_creation_input_tokens=int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            ),
            cache_read_input_tokens=int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            ),
        )


def _translate_api_error(exc: APIError) -> OpenHarnessApiError:
    # Structural classification lives in one place; this wrapper only keeps the
    # historical entry point used across the module.
    return classify_api_error(exc, type_name=exc.__class__.__name__)


# Names resolved from the lazily imported SDK.  Keeping them out of module
# globals is what makes ``import openharness.api.client`` cheap; module-level
# ``__getattr__`` (PEP 562) still resolves them on demand, which keeps
# ``monkeypatch.setattr("openharness.api.client.AsyncAnthropic", ...)`` working.
_LAZY_SDK_NAMES = frozenset({"APIError", "APIStatusError", "AsyncAnthropic"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_SDK_NAMES:
        return getattr(_anthropic_sdk(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
