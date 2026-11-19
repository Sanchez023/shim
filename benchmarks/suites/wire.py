"""Offline provider wire-layer suite.

These tasks drive the real API clients against a fake ``httpx`` transport, and
the real query engine against a fake streaming client.  Nothing here contacts a
provider: every response is a deterministic in-process fixture.

Assertions are about the seam between the harness and its provider:

* retry backoff honours ``Retry-After`` while remaining testable without sleep;
* streamed text and tool arguments accumulate correctly across chunks;
* truncated tool arguments fail loudly instead of silently becoming ``{}``;
* a ``context_length_exceeded`` failure triggers exactly one reactive compact.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

try:  # Current SDK releases transport through httpx2.
    import httpx2 as httpx
except ImportError:  # pragma: no cover - older SDKs use httpx
    import httpx

from benchmarks.spec import BenchRun, CollectingEventSink, Task
from openharness.api.client import AnthropicApiClient
from openharness.api.errors import RequestFailure
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import Settings
from openharness.engine.messages import ConversationMessage, ToolUseBlock
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import AssistantTurnComplete, ErrorEvent
from openharness.permissions import PermissionChecker
from openharness.services.compact_port import ServiceCompactPort
from openharness.tools import create_default_tool_registry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _anthropic_request() -> Any:
    from openharness.api.client import ApiMessageRequest

    return ApiMessageRequest(
        model="claude-test",
        messages=[ConversationMessage.from_user_text("Say OK.")],
        max_tokens=64,
    )


def _openai_request(tools: list[dict[str, Any]] | None = None) -> Any:
    from openharness.api.client import ApiMessageRequest

    return ApiMessageRequest(
        model="gpt-4o-mini",
        messages=[ConversationMessage.from_user_text("Read the file.")],
        max_tokens=64,
        tools=tools or [],
    )


def _anthropic_sse(text: str) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
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
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


def _openai_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _openai_sse(
    chunks: list[dict[str, Any]],
    *,
    usage: dict[str, int] | None = None,
) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if usage is not None:
        lines.append(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "gpt-4o-mini",
                    "choices": [],
                    "usage": usage,
                }
            )
            + "\n\n"
        )
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


def _record(
    task: Task,
    *,
    details: dict[str, Any] | None = None,
    error: str | None = None,
    duration: float = 0.0,
    usage: UsageSnapshot | None = None,
) -> BenchRun:
    return BenchRun(
        task=task,
        workspace=Path(tempfile.gettempdir()),
        events=[],
        tools=[],
        results=[],
        permission_prompts=[],
        questions=[],
        answers=[],
        usage=usage or UsageSnapshot(),
        duration_seconds=duration,
        turns=0,
        error=error,
        details=dict(details or {}),
    )


# ---------------------------------------------------------------------------
# retry-after-honored
# ---------------------------------------------------------------------------


async def _run_retry_after(task: Task) -> BenchRun:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "1"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            )
        return _sse_response(_anthropic_sse("RECOVERED"))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicApiClient(
        api_key="test-key",
        base_url="https://mock.local",
        http_client=http_client,
        max_retries=1,
        retry_delay_factor=0.0,
    )
    started = time.monotonic()
    events: list[Any] = []
    error: str | None = None
    try:
        events = [event async for event in client.stream_message(_anthropic_request())]
    except Exception as exc:  # noqa: BLE001 - recorded as benchmark evidence
        error = f"{type(exc).__name__}: {exc}"
    finally:
        duration = time.monotonic() - started
        await client.close()

    retry_delays = [
        event.delay_seconds
        for event in events
        if type(event).__name__ == "ApiRetryEvent"
    ]
    final_text = ""
    for event in reversed(events):
        message = getattr(event, "message", None)
        if message is not None:
            final_text = message.text
            break
    return _record(
        task,
        details={
            "calls": state["calls"],
            "retry_delays": retry_delays,
            "final_text": final_text,
        },
        error=error,
        duration=duration,
    )


def _grade_retry_after(run: BenchRun) -> list[str]:
    failures: list[str] = []
    delays = run.details.get("retry_delays", [])
    if delays != [1.0]:
        failures.append(f"expected one retry with delay 1.0s, saw {delays!r}")
    if run.details.get("calls") != 2:
        failures.append(f"expected two provider calls, saw {run.details.get('calls')}")
    if run.details.get("final_text") != "RECOVERED":
        failures.append(f"retry did not recover: {run.details.get('final_text')!r}")
    # The client is configured with retry_delay_factor=0.0, so it must not
    # sleep.  Keep headroom for a loaded CI machine: an actual 1s sleep is
    # still far above this bound, while scheduler stalls no longer fail it.
    if run.duration_seconds >= 0.75:
        failures.append(f"retry actually slept: {run.duration_seconds:.3f}s")
    return failures


# ---------------------------------------------------------------------------
# stream-chunks-accumulate
# ---------------------------------------------------------------------------


async def _run_stream_chunks(task: Task) -> BenchRun:
    chunks = [
        _openai_chunk({"content": "Hel"}),
        _openai_chunk({"content": "lo "}),
        _openai_chunk({"content": "world"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"pa'},
                    }
                ]
            }
        ),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": 'th": "a.txt"}'},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    body = _openai_sse(
        chunks,
        usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return _sse_response(body)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://mock.local/v1",
        http_client=http_client,
        retry_delay_factor=0.0,
    )
    text_deltas: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    final_text = ""
    complete_count = 0
    error: str | None = None
    try:
        request = _openai_request(
            tools=[{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}]
        )
        async for event in client.stream_message(request):
            if type(event).__name__ == "ApiTextDeltaEvent":
                text_deltas.append(event.text)
            elif type(event).__name__ == "ApiMessageCompleteEvent":
                complete_count += 1
                final_text = event.message.text
                tool_uses = [
                    {"name": block.name, "input": dict(block.input)}
                    for block in event.message.content
                    if isinstance(block, ToolUseBlock)
                ]
    except Exception as exc:  # noqa: BLE001 - recorded as benchmark evidence
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()

    return _record(
        task,
        details={
            "text_deltas": text_deltas,
            "final_text": final_text,
            "tool_uses": tool_uses,
            "complete_count": complete_count,
        },
        error=error,
    )


def _grade_stream_chunks(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.details.get("text_deltas") != ["Hel", "lo ", "world"]:
        failures.append(f"text deltas were not streamed intact: {run.details.get('text_deltas')!r}")
    if run.details.get("final_text") != "Hello world":
        failures.append(f"accumulated text was {run.details.get('final_text')!r}")
    tool_uses = run.details.get("tool_uses") or []
    if len(tool_uses) != 1:
        failures.append(f"expected one tool use, saw {tool_uses!r}")
    else:
        tool = tool_uses[0]
        if tool["name"] != "read_file":
            failures.append(f"tool name was {tool['name']!r}")
        if tool["input"] != {"path": "a.txt"}:
            failures.append(f"tool arguments did not stitch across chunks: {tool['input']!r}")
    if run.details.get("complete_count") != 1:
        failures.append(f"expected one complete event, saw {run.details.get('complete_count')}")
    return failures


# ---------------------------------------------------------------------------
# malformed-tool-args
# ---------------------------------------------------------------------------


async def _run_malformed_tool_args(task: Task) -> BenchRun:
    chunks = [
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.tx'},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    body = _openai_sse(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return _sse_response(body)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://mock.local/v1",
        http_client=http_client,
        retry_delay_factor=0.0,
    )
    error_type = ""
    error_message = ""
    complete_count = 0
    try:
        request = _openai_request(
            tools=[{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}]
        )
        async for event in client.stream_message(request):
            if type(event).__name__ == "ApiMessageCompleteEvent":
                complete_count += 1
    except Exception as exc:  # noqa: BLE001 - the failure is the assertion target
        error_type = type(exc).__name__
        error_message = str(exc)
    finally:
        await client.close()

    return _record(
        task,
        details={
            "error_type": error_type,
            "error_message": error_message,
            "complete_count": complete_count,
        },
    )


def _grade_malformed_tool_args(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.details.get("error_type") != "RequestFailure":
        failures.append(
            f"expected a RequestFailure for truncated arguments, saw "
            f"{run.details.get('error_type')!r}"
        )
    message = str(run.details.get("error_message", ""))
    if "Malformed tool arguments" not in message:
        failures.append(f"the error was not diagnosable: {message!r}")
    if "read_file" not in message:
        failures.append(f"the error did not name the tool: {message!r}")
    if run.details.get("complete_count") != 0:
        failures.append("a truncated tool call still produced a complete message")
    return failures


# ---------------------------------------------------------------------------
# prompt-too-long-reactive
# ---------------------------------------------------------------------------


class _PromptTooLongClient:
    """Fails with a context-length error until enough calls have been made."""

    def __init__(self, *, fail_calls: int) -> None:
        self._fail_calls = fail_calls
        self.calls = 0

    async def stream_message(self, request: Any):
        del request
        self.calls += 1
        if self.calls <= self._fail_calls:
            raise RequestFailure("context_length_exceeded: prompt is too long")
        from openharness.api.client import ApiMessageCompleteEvent
        from openharness.engine.messages import TextBlock

        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="recovered after compaction")],
            ),
            usage=UsageSnapshot(input_tokens=3, output_tokens=4),
            stop_reason="end_turn",
        )


async def _exercise_prompt_too_long(fail_calls: int) -> dict[str, Any]:
    client = _PromptTooLongClient(fail_calls=fail_calls)
    settings = Settings(
        model="fake-wire",
        max_turns=5,
        permission={"mode": "full_auto"},
        memory={"enabled": False},
    )
    sink = CollectingEventSink()
    engine = QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(settings.permission),
        cwd=Path(tempfile.gettempdir()),
        model=settings.model,
        system_prompt="You are an offline wire-layer test harness.",
        max_tokens=64,
        max_turns=5,
        settings=settings,
        event_sink=sink,
        auto_compact_threshold_tokens=None,
        compact_port=ServiceCompactPort(),
    )
    events: list[Any] = []
    error: str | None = None
    try:
        async for event in engine.submit_message("do it"):
            events.append(event)
    except Exception as exc:  # noqa: BLE001 - recorded as benchmark evidence
        error = f"{type(exc).__name__}: {exc}"

    compacted = [
        event.payload.get("trigger")
        for event in sink.events
        if getattr(event, "kind", "") == "context.compacted"
    ]
    error_messages = [event.message for event in events if isinstance(event, ErrorEvent)]
    final_text = ""
    for event in reversed(events):
        if isinstance(event, AssistantTurnComplete):
            final_text = event.message.text
            break
    return {
        "calls": client.calls,
        "compacted_triggers": compacted,
        "error_messages": error_messages,
        "final_text": final_text,
        "engine_error": error,
    }


async def _run_prompt_too_long_recovery(task: Task) -> BenchRun:
    details = await _exercise_prompt_too_long(fail_calls=1)
    return _record(task, details={"scenario": details})


async def _run_prompt_too_long_exhausted(task: Task) -> BenchRun:
    details = await _exercise_prompt_too_long(fail_calls=99)
    return _record(task, details={"scenario": details})


def _grade_prompt_too_long_recovery(run: BenchRun) -> list[str]:
    failures: list[str] = []
    scenario = run.details.get("scenario", {})
    if scenario.get("engine_error") is not None:
        failures.append(f"recovery run raised: {scenario.get('engine_error')}")
    if scenario.get("compacted_triggers") != ["reactive"]:
        failures.append(
            "expected exactly one reactive compaction, saw "
            f"{scenario.get('compacted_triggers')!r}"
        )
    if scenario.get("final_text") != "recovered after compaction":
        failures.append(f"recovery did not finish: {scenario.get('final_text')!r}")
    if scenario.get("calls") != 2:
        failures.append(f"expected two provider calls after recovery, saw {scenario.get('calls')}")
    return failures


def _grade_prompt_too_long_exhausted(run: BenchRun) -> list[str]:
    failures: list[str] = []
    scenario = run.details.get("scenario", {})
    if scenario.get("engine_error") is not None:
        failures.append(
            "the repeated-failure run escaped as an exception instead of a terminal error: "
            f"{scenario.get('engine_error')}"
        )
    if scenario.get("calls") != 2:
        failures.append(
            f"reactive compaction retried more than once: {scenario.get('calls')} calls"
        )
    if not scenario.get("error_messages"):
        failures.append("the second context-length failure produced no error event")
    elif not any("context_length_exceeded" in str(msg) for msg in scenario["error_messages"]):
        failures.append(f"the terminal error lost its cause: {scenario['error_messages']}")
    if scenario.get("final_text"):
        failures.append("the repeated-failure run produced a final answer")
    return failures


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


WIRE_TASKS: tuple[Task, ...] = (
    Task(
        id="retry-after-honored",
        description="A 429 Retry-After delay is reported and retried without real sleeping",
        prompt="Say OK.",
        runner=_run_retry_after,
        grade=_grade_retry_after,
        tags=("wire", "retry"),
    ),
    Task(
        id="stream-chunks-accumulate",
        description="Streamed text and split tool arguments accumulate into one message",
        prompt="Read the file.",
        runner=_run_stream_chunks,
        grade=_grade_stream_chunks,
        tags=("wire", "stream"),
    ),
    Task(
        id="malformed-tool-args",
        description="Truncated tool-argument JSON raises a diagnosable error, not an empty input",
        prompt="Read the file.",
        runner=_run_malformed_tool_args,
        grade=_grade_malformed_tool_args,
        tags=("wire", "stream", "robustness"),
    ),
    Task(
        id="prompt-too-long-reactive",
        description="A context-length failure triggers exactly one reactive compact, then succeeds",
        prompt="Do it.",
        runner=_run_prompt_too_long_recovery,
        grade=_grade_prompt_too_long_recovery,
        tags=("wire", "compaction"),
    ),
    Task(
        id="prompt-too-long-exhausted",
        description="A second context-length failure ends the turn instead of retrying again",
        prompt="Do it.",
        runner=_run_prompt_too_long_exhausted,
        grade=_grade_prompt_too_long_exhausted,
        tags=("wire", "compaction"),
    ),
)

__all__ = ["WIRE_TASKS"]
