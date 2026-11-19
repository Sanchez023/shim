"""Contracts for the shared streaming-retry core."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from openharness.api.transport import (
    RetryPolicy,
    StreamAlreadyStarted,
    is_transient,
    retry_delay,
    stream_with_retry,
)

FAST = RetryPolicy(max_retries=2, base_delay=0.0, jitter_ratio=0.0)


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _scripted(failures: list[BaseException], events_per_attempt: int) -> object:
    """Return an open_stream factory that fails *failures.pop(0)* per attempt."""
    calls = {"n": 0}

    async def _gen() -> AsyncIterator[str]:
        index = calls["n"]
        calls["n"] += 1
        for i in range(events_per_attempt):
            yield f"chunk-{index}-{i}"
        if failures:
            raise failures.pop(0)

    def _open() -> AsyncIterator[str]:
        return _gen()

    return _open, calls


@pytest.mark.asyncio
async def test_stream_that_already_emitted_is_not_replayed() -> None:
    """A mid-stream failure must surface, never restart from zero."""
    open_stream, calls = _scripted([FakeStatusError(503)], events_per_attempt=2)

    received: list[str] = []
    with pytest.raises(StreamAlreadyStarted):
        async for event in stream_with_retry(open_stream, policy=FAST):
            received.append(event)

    assert received == ["chunk-0-0", "chunk-0-1"]
    assert calls["n"] == 1, "a partially delivered stream was retried"


@pytest.mark.asyncio
async def test_failure_before_first_event_is_retried() -> None:
    open_stream, calls = _scripted([FakeStatusError(429)], events_per_attempt=0)

    received: list[str] = []
    async for event in stream_with_retry(open_stream, policy=FAST):
        received.append(event)

    assert calls["n"] == 2
    assert received == []


@pytest.mark.asyncio
async def test_retry_event_is_emitted_through_callback() -> None:
    open_stream, _ = _scripted([FakeStatusError(503)], events_per_attempt=0)

    notices: list[tuple[int, int]] = []

    def _collect(notice) -> str:
        notices.append((notice.attempt, notice.max_attempts))
        return "retry"

    async for _ in stream_with_retry(open_stream, policy=FAST, on_retry=_collect):
        pass
    assert notices == [(1, 3)]


@pytest.mark.asyncio
async def test_non_retryable_error_is_raised_immediately() -> None:
    open_stream, calls = _scripted([FakeStatusError(400)], events_per_attempt=0)

    with pytest.raises(FakeStatusError):
        async for _ in stream_with_retry(open_stream, policy=FAST):
            pass
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_exhausted_retries_raise_translated_error() -> None:
    open_stream, calls = _scripted(
        [FakeStatusError(503), FakeStatusError(503), FakeStatusError(503)],
        events_per_attempt=0,
    )

    with pytest.raises(RuntimeError, match="translated"):
        async for _ in stream_with_retry(
            open_stream,
            policy=FAST,
            translate_error=lambda exc: RuntimeError(f"translated: {exc}"),
        ):
            pass
    assert calls["n"] == 3


# --- retryability ----------------------------------------------------------


class APIConnectionError(Exception):
    """Mirrors the SDK class name the transport recognises structurally."""


@pytest.mark.parametrize(
    "exc",
    [
        APIConnectionError("dns failure"),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        FakeStatusError(429),
        FakeStatusError(503),
        ConnectionResetError(),
        TimeoutError(),
    ],
)
def test_transient_errors_are_retryable(exc: BaseException) -> None:
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [FakeStatusError(400), FakeStatusError(401), FakeStatusError(404), ValueError("bad")],
)
def test_client_errors_are_not_retryable(exc: BaseException) -> None:
    assert is_transient(exc) is False


def test_wrapped_transport_error_is_detected_through_cause() -> None:
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise ValueError("wrapped") from inner
    except ValueError as outer:
        assert is_transient(outer) is True


def test_retry_after_header_overrides_backoff() -> None:
    exc = httpx.HTTPStatusError(
        "rate limited",
        request=httpx.Request("POST", "https://example.invalid"),
        response=httpx.Response(429, headers={"retry-after": "7"}),
    )
    assert retry_delay(RetryPolicy(), 0, exc) == 7.0


# --- ADR-0012 D6: one retry predicate ---------------------------------------


def test_typed_taxonomy_errors_report_themselves_retryable() -> None:
    from openharness.api.errors import ConnectionFailure, OverloadFailure

    assert is_transient(OverloadFailure("busy")) is True
    assert is_transient(ConnectionFailure("reset")) is True


def test_adapters_share_one_retry_predicate() -> None:
    """The three status-code sets had already drifted (529 on one path only)."""
    import openharness.api.client as anthropic_client
    import openharness.api.codex_client as codex_client
    import openharness.api.openai_client as openai_client
    from openharness.api import transport

    # One set, owned by the transport; adapters only ever re-export it.
    assert anthropic_client.RETRYABLE_STATUS_CODES is transport.RETRYABLE_STATUS_CODES
    assert not hasattr(openai_client, "RETRYABLE_STATUS_CODES")
    assert not hasattr(codex_client, "RETRYABLE_STATUS_CODES")
    assert not hasattr(anthropic_client, "_is_retryable")
    assert not hasattr(openai_client.OpenAICompatibleClient, "_is_retryable")
    assert not hasattr(codex_client.CodexApiClient, "_is_retryable")
