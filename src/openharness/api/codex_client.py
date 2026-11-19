"""OpenAI Codex subscription client backed by chatgpt.com Codex Responses."""

from __future__ import annotations

import base64
import json
import platform
from collections.abc import AsyncIterator
from typing import Any

import httpx

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
    AuthenticationFailure,
    OpenHarnessApiError,
    RateLimitFailure,
    RequestFailure,
    classify_api_error,
    classify_message,
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

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0


def _extract_account_id(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationFailure("Codex access token is not a valid JWT.")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
        )
    except Exception as exc:
        raise AuthenticationFailure("Could not decode Codex access token payload.") from exc
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if not isinstance(auth_claim, dict):
        raise AuthenticationFailure("Codex access token is missing account metadata.")
    account_id = auth_claim.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise AuthenticationFailure("Codex access token is missing chatgpt_account_id.")
    return account_id


def _resolve_codex_url(base_url: str | None) -> str:
    trimmed = (base_url or "").strip()
    if trimmed and "chatgpt.com/backend-api" not in trimmed:
        trimmed = ""
    raw = (trimmed or DEFAULT_CODEX_BASE_URL).rstrip("/")
    if raw.endswith("/codex/responses"):
        return raw
    if raw.endswith("/codex"):
        return f"{raw}/responses"
    return f"{raw}/codex/responses"


def _build_codex_headers(token: str, *, session_id: str | None = None) -> dict[str, str]:
    account_id = _extract_account_id(token)
    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "originator": "openharness",
        "User-Agent": f"openharness ({platform.system().lower()} {platform.machine() or 'unknown'})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if session_id:
        headers["session_id"] = session_id
    return headers


def _convert_messages_to_codex(messages: list[ConversationMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            # Responses API requires function_call_output items to appear before
            # any following user input.  A ConversationMessage can contain both
            # tool results and user text, so emit the tool outputs first to keep
            # every prior function_call immediately satisfied.
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    result.append({
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": block.content,
                    })
            user_content: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    user_content.append({"type": "input_text", "text": block.text})
                elif isinstance(block, ImageBlock):
                    user_content.append({
                        "type": "input_image",
                        "image_url": f"data:{block.media_type};base64,{block.data}",
                    })
            if user_content:
                result.append({"role": "user", "content": user_content})
            continue

        # Reasoning items must precede the assistant output they produced.
        for block in msg.content:
            if isinstance(block, ReasoningBlock) and block.provider in {"codex", "openai", ""}:
                item = _reasoning_block_to_codex_item(block)
                if item is not None:
                    result.append(item)

        assistant_text = "".join(block.text for block in msg.content if isinstance(block, TextBlock))
        if assistant_text:
            result.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text, "annotations": []}],
            })
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                result.append({
                    "type": "function_call",
                    "id": block.provider_item_id or f"fc_{block.id[:58]}",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, separators=(",", ":")),
                })
    return result


def _reasoning_block_to_codex_item(block: ReasoningBlock) -> dict[str, Any] | None:
    """Replay a reasoning block as the original Responses item when possible."""
    raw = block.raw if isinstance(block.raw, dict) else {}
    if raw.get("type") == "reasoning":
        return dict(raw)

    summary = [
        {"type": "summary_text", "text": text}
        for text in block.summary
        if isinstance(text, str) and text
    ]
    if not block.item_id and not summary and not block.encrypted_content:
        return None

    item: dict[str, Any] = {"type": "reasoning"}
    if block.item_id:
        item["id"] = block.item_id
    if summary:
        item["summary"] = summary
    if block.encrypted_content:
        item["encrypted_content"] = block.encrypted_content
    return item


def _reasoning_block_from_item(item: dict[str, Any]) -> ReasoningBlock:
    """Capture a Codex reasoning item without losing opaque state."""
    summary: list[str] = []
    for key in ("summary", "content"):
        raw_parts = item.get(key)
        if not isinstance(raw_parts, list):
            continue
        for part in raw_parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in {"summary_text", "reasoning_text"}:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                summary.append(text)

    item_id = item.get("id")
    encrypted = item.get("encrypted_content")
    return ReasoningBlock(
        provider="codex",
        item_id=item_id if isinstance(item_id, str) else "",
        summary=summary,
        encrypted_content=encrypted if isinstance(encrypted, str) else "",
        raw=dict(item),
    )


def _convert_tools_to_codex(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        }
        for tool in tools
    ]


def _normalize_reasoning_effort(effort: str | None) -> str | None:
    normalized = (effort or "").strip().lower()
    if normalized == "max":
        return "xhigh"
    if normalized in {"low", "medium", "high", "xhigh"}:
        return normalized
    return None


def _usage_from_response(response: dict[str, Any]) -> UsageSnapshot:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return UsageSnapshot()
    prompt_tokens = int(usage.get("input_tokens") or 0)
    details = usage.get("input_tokens_details")
    cached = (
        int(details.get("cached_tokens") or 0)
        if isinstance(details, dict)
        else 0
    )
    cached = min(max(cached, 0), prompt_tokens)
    return UsageSnapshot(
        input_tokens=prompt_tokens - cached,
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_input_tokens=cached,
    )


def _stop_reason_from_response(response: dict[str, Any], *, has_tool_calls: bool) -> str | None:
    status = response.get("status")
    if has_tool_calls and status == "completed":
        return "tool_use"
    if status == "completed":
        return "stop"
    if status == "incomplete":
        return "length"
    if status in {"failed", "cancelled"}:
        return "error"
    return None


def _format_error_message(status_code: int, payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
        detail = parsed.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
    text = payload.strip()
    if text:
        return text
    return f"Codex request failed with status {status_code}"


def _format_codex_stream_error(event: dict[str, Any], *, fallback: str) -> str:
    error = event.get("error")
    payload = error if isinstance(error, dict) else event
    message = payload.get("message") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    request_id = (
        (payload.get("request_id") if isinstance(payload, dict) else None)
        or event.get("request_id")
    )

    parts: list[str] = []
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    elif isinstance(code, str) and code.strip():
        parts.append(code.strip())
    else:
        parts.append(fallback)

    if isinstance(code, str) and code.strip():
        parts.append(f"(code={code.strip()})")
    if isinstance(request_id, str) and request_id.strip():
        parts.append(f"[request_id={request_id.strip()}]")
    return " ".join(parts)


def _translate_status_error(status_code: int, message: str) -> OpenHarnessApiError:
    if status_code in {401, 403}:
        return AuthenticationFailure(message)
    if status_code == 429:
        return RateLimitFailure(message)
    return RequestFailure(message)


class CodexApiClient:
    """Client for ChatGPT/Codex subscription-backed Codex Responses."""

    def __init__(self, auth_token: str, *, base_url: str | None = None) -> None:
        self._auth_token = auth_token
        self._base_url = base_url
        self._url = _resolve_codex_url(base_url)

    def capabilities(self) -> ModelCapabilities:
        """Declare Codex Responses streaming, tools, vision and reasoning support."""
        return ModelCapabilities(
            streaming=True,
            tools=True,
            images=True,
            reasoning=True,
            native_reasoning_state=True,
        )

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        async for event in stream_with_retry(
            lambda: self._stream_once(request),
            policy=RetryPolicy(max_retries=MAX_RETRIES),
            translate_error=self._translate_error,
            on_retry=lambda notice: ApiRetryEvent(
                message=notice.message,
                attempt=notice.attempt,
                max_attempts=notice.max_attempts,
                delay_seconds=notice.delay_seconds,
            ),
            label="Codex",
        ):
            yield event

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        body: dict[str, Any] = {
            "model": request.model,
            "store": False,
            "stream": True,
            "instructions": request.system_prompt or "You are OpenHarness.",
            "input": _convert_messages_to_codex(request.messages),
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if request.tools:
            body["tools"] = _convert_tools_to_codex(request.tools)
        effort = _normalize_reasoning_effort(request.effort)
        if effort:
            body["reasoning"] = {"effort": effort}

        content: list[ContentBlock] = []
        current_text_parts: list[str] = []
        completed_response: dict[str, Any] | None = None

        headers = _build_codex_headers(self._auth_token)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("POST", self._url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    payload = await response.aread()
                    message = _format_error_message(response.status_code, payload.decode("utf-8", "replace"))
                    raise httpx.HTTPStatusError(message, request=response.request, response=response)

                async for event in self._iter_sse_events(response):
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            current_text_parts.append(delta)
                            yield ApiTextDeltaEvent(text=delta)
                    elif event_type in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield ApiThinkingDeltaEvent(text=delta)
                    elif event_type == "response.output_item.done":
                        item = event.get("item")
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("type")
                        if item_type == "message":
                            text = ""
                            raw_content = item.get("content")
                            if isinstance(raw_content, list):
                                parts = []
                                for block in raw_content:
                                    if isinstance(block, dict):
                                        if block.get("type") == "output_text":
                                            parts.append(str(block.get("text", "")))
                                        elif block.get("type") == "refusal":
                                            parts.append(str(block.get("refusal", "")))
                                text = "".join(parts)
                            if text:
                                content.append(TextBlock(text=text))
                        elif item_type == "function_call":
                            arguments = item.get("arguments")
                            parsed_arguments: dict[str, Any]
                            if isinstance(arguments, str) and arguments:
                                try:
                                    loaded = json.loads(arguments)
                                except json.JSONDecodeError:
                                    loaded = {}
                            else:
                                loaded = {}
                            parsed_arguments = loaded if isinstance(loaded, dict) else {}
                            call_id = item.get("call_id")
                            name = item.get("name")
                            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                                provider_item_id = item.get("id")
                                content.append(
                                    ToolUseBlock(
                                        id=call_id,
                                        name=name,
                                        input=parsed_arguments,
                                        provider_item_id=(
                                            provider_item_id
                                            if isinstance(provider_item_id, str)
                                            else ""
                                        ),
                                    )
                                )
                        elif item_type == "reasoning":
                            content.append(_reasoning_block_from_item(item))
                    elif event_type == "response.completed":
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            completed_response = response_payload
                    elif event_type == "response.failed":
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            raise classify_message(
                                _format_codex_stream_error(
                                    response_payload,
                                    fallback="Codex response failed",
                                )
                            )
                        raise classify_message("Codex response failed")
                    elif event_type == "error":
                        raise classify_message(
                            _format_codex_stream_error(event, fallback="Codex error")
                        )

        if current_text_parts and not any(isinstance(block, TextBlock) for block in content):
            content.insert(0, TextBlock(text="".join(current_text_parts)))

        final_message = ConversationMessage(role="assistant", content=content)
        usage = _usage_from_response(completed_response or {})
        stop_reason = _stop_reason_from_response(
            completed_response or {},
            has_tool_calls=bool(final_message.tool_uses),
        )
        yield ApiMessageCompleteEvent(
            message=final_message,
            usage=usage,
            stop_reason=stop_reason,
            finish_reason=normalize_finish_reason(stop_reason),
        )

    async def _iter_sse_events(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines).strip()
                    data_lines = []
                    if payload and payload != "[DONE]":
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            yield event
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            payload = "\n".join(data_lines).strip()
            if payload and payload != "[DONE]":
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    return
                if isinstance(event, dict):
                    yield event

    @staticmethod
    def _translate_error(exc: Exception) -> OpenHarnessApiError:
        if isinstance(exc, OpenHarnessApiError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            return _translate_status_error(exc.response.status_code, str(exc))
        return classify_api_error(exc, type_name=exc.__class__.__name__)
