"""Shared streaming-retry core for every provider adapter.

Before this module each adapter carried its own retry loop, and each loop had
the same two defects:

* The retry wrapped the *whole* generator, so a failure halfway through a
  stream replayed every delta from zero -- duplicated text in the UI and a
  second full prompt billed to the user.
* The retryable predicate only understood a narrow set of transport errors;
  ``APIConnectionError`` (a DNS blip) was classified as fatal.

Both are fixed here: a stream that has already emitted an event is never
replayed, and retryability is decided structurally (status codes, transport
exceptions and their causes) rather than by provider-specific classes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

log = logging.getLogger(__name__)

BASE_DELAY = 1.0
MAX_DELAY = 30.0
MAX_RETRIES = 3
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# SDK exception types that mean "the request never produced a usable response".
# Checked by name so this module does not have to import every provider SDK.
_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "InternalServerError",
        "OverloadedError",
        "PoolTimeout",
        "RateLimitError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "ServiceUnavailableError",
        "Timeout",
        "TimeoutException",
        "WriteError",
    }
)

T = TypeVar("T")


class StreamAlreadyStarted(RuntimeError):
    """Raised when a stream fails after producing output.

    The caller must surface this as an error: replaying the request would
    duplicate deltas the user already saw and pay for the prompt twice.
    """


@dataclass(frozen=True)
class RetryPolicy:
    """Retry knobs.  ``delay_factor`` scales every sleep (config/CLI override)."""

    max_retries: int = MAX_RETRIES
    base_delay: float = BASE_DELAY
    max_delay: float = MAX_DELAY
    jitter_ratio: float = 0.25
    delay_factor: float = 1.0


@dataclass(frozen=True)
class RetryNotice:
    """Information about one scheduled retry, for the UI/log."""

    message: str
    attempt: int
    max_attempts: int
    delay_seconds: float


def is_transient(exc: Exception, *, depth: int = 0) -> bool:
    """Decide whether *exc* is worth retrying.

    Structural, not textual: transport failures, retryable HTTP statuses, and
    SDK wrapper exceptions.  ``__cause__`` is inspected because the SDKs wrap
    ``httpx`` errors in their own classes.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    # Typed taxonomy instances carry the answer themselves (ADR-0012 D6).
    if getattr(exc, "retryable", False):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if type(exc).__name__ in _TRANSIENT_EXCEPTION_NAMES:
        return True

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS_CODES

    if depth < 2:
        cause = exc.__cause__
        if isinstance(cause, Exception):
            return is_transient(cause, depth=depth + 1)
    return False


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return min(float(str(value)), MAX_DELAY)
    except (TypeError, ValueError):
        return None


def retry_delay(policy: RetryPolicy, attempt: int, exc: Exception | None = None) -> float:
    """Exponential backoff with jitter, honouring ``Retry-After``."""
    if exc is not None:
        hinted = _retry_after_seconds(exc)
        if hinted is not None:
            return hinted
    delay = min(policy.base_delay * (2**attempt), policy.max_delay)
    jitter = random.uniform(0, delay * max(0.0, policy.jitter_ratio))
    return float(delay + jitter)


async def stream_with_retry(
    open_stream: Callable[[], AsyncIterator[T]],
    *,
    policy: RetryPolicy | None = None,
    is_retryable: Callable[[Exception], bool] = is_transient,
    translate_error: Callable[[Exception], Exception] | None = None,
    on_retry: Callable[[RetryNotice], T] | None = None,
    label: str = "API",
) -> AsyncIterator[T]:
    """Stream from ``open_stream`` with retries that never replay output.

    ``open_stream`` is a zero-argument factory: it is invoked once per attempt,
    so it may rebuild client state (auth refresh, new connection) each time.
    ``on_retry`` returns an event to yield, so adapters keep their own event
    types without this module importing them.
    """
    policy = policy or RetryPolicy()
    attempts = max(0, policy.max_retries) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        emitted = False
        try:
            async for event in open_stream():
                emitted = True
                yield event
            return
        except Exception as exc:
            last_error = exc
            if emitted:
                raise StreamAlreadyStarted(str(exc) or type(exc).__name__) from exc
            if attempt >= attempts - 1 or not is_retryable(exc):
                raise _translate(translate_error, exc) from exc

            delay = retry_delay(policy, attempt, exc)
            log.warning(
                "%s request failed (attempt %d/%d), retrying in %.1fs: %s",
                label,
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            if on_retry is not None:
                yield on_retry(
                    RetryNotice(
                        message=str(exc) or type(exc).__name__,
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        delay_seconds=delay,
                    )
                )
            # ``delay_factor`` scales the actual sleep without changing the
            # delay reported to the UI (tests and benchmarks run it at 0).
            await asyncio.sleep(delay * max(0.0, policy.delay_factor))

    if last_error is not None:  # pragma: no cover - loop always returns or raises
        raise _translate(translate_error, last_error) from last_error


def _translate(
    translate_error: Callable[[Exception], Exception] | None,
    exc: Exception,
) -> Exception:
    if translate_error is None:
        return exc
    return translate_error(exc)
