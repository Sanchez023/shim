"""API error types for OpenHarness.

Errors are classified structurally (status code, exception type name, transport
cause) rather than by matching provider prose.  Prose matching still exists as a
last-resort fallback for OpenAI-compatible servers that return HTTP 400 with a
message, but it lives here instead of being duplicated across the engine.
"""

from __future__ import annotations

import re

from openharness.api.transport import is_transient


class OpenHarnessApiError(RuntimeError):
    """Base class for upstream API failures."""

    #: Safe to retry the same request unchanged.
    retryable: bool = False
    #: HTTP status, when the failure came from a response.
    status_code: int | None = None


class AuthenticationFailure(OpenHarnessApiError):
    """Raised when the upstream service rejects the provided credentials."""


class PermissionFailure(OpenHarnessApiError):
    """Raised when the credentials are valid but lack access to the model."""


class RateLimitFailure(OpenHarnessApiError):
    """Raised when the upstream service rejects the request due to rate limits."""

    retryable = True


class OverloadFailure(OpenHarnessApiError):
    """Raised when the provider is temporarily overloaded (HTTP 503/529)."""

    retryable = True


class ConnectionFailure(OpenHarnessApiError):
    """Raised when the request never reached a usable response (DNS, reset)."""

    retryable = True


class ContextLengthFailure(OpenHarnessApiError):
    """Raised when the request exceeds the model's context window."""


class CompletionTokenLimitFailure(OpenHarnessApiError):
    """Raised when ``max_tokens`` exceeds what the model accepts."""

    def __init__(self, message: str, *, supported_limit: int | None = None) -> None:
        super().__init__(message)
        self.supported_limit = supported_limit


class RequestFailure(OpenHarnessApiError):
    """Raised for generic request or transport failures."""


_CONTEXT_LENGTH_NEEDLES = (
    "prompt too long",
    "context_length_exceeded",
    "context length",
    "maximum context",
    "context window",
    "input tokens exceed",
    "messages resulted in",
    "reduce the length of the messages",
    "configured limit",
    "too many tokens",
    "too large for the model",
    "maximum context length",
    "exceed_context",
    "exceeds the available context size",
    "available context size",
)

#: Provider prose that means "the request never produced an answer".  Used only
#: as a last resort, for in-stream failures that carry no status code.
_RETRYABLE_PROSE = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection error",
    "network error",
    "temporarily unavailable",
    "overloaded",
    "rate limit",
    "too many requests",
)

_COMPLETION_LIMIT_PATTERNS = (
    re.compile(r"supports at most\s+(\d+)\s+completion tokens"),
    re.compile(r"at most\s+(\d+)\s+completion tokens"),
    re.compile(
        r"max(?:imum)?(?:_completion)?[_\s-]tokens.*?(?:<=|less than or equal to|at most)\s+(\d+)"
    ),
)


def completion_token_limit(exc: Exception) -> int | None:
    """Return the provider's stated completion-token ceiling, if any."""
    if isinstance(exc, CompletionTokenLimitFailure):
        return exc.supported_limit
    text = str(exc).lower().replace(",", "")
    for pattern in _COMPLETION_LIMIT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return max(1, int(match.group(1)))
            except ValueError:
                return None
    return None


def is_context_length_error(exc: Exception) -> bool:
    """True when the provider rejected the prompt for being too long."""
    if isinstance(exc, ContextLengthFailure):
        return True
    text = str(exc).lower()
    return any(needle in text for needle in _CONTEXT_LENGTH_NEEDLES)


def is_completion_token_limit_error(exc: Exception) -> bool:
    """True when the provider rejected the requested ``max_tokens`` value."""
    if isinstance(exc, CompletionTokenLimitFailure):
        return True
    text = str(exc).lower()
    return (
        ("max_tokens" in text or "max_completion_tokens" in text)
        and ("too large" in text or "at most" in text or "completion tokens" in text)
    )


def classify_api_error(
    exc: Exception,
    *,
    type_name: str | None = None,
) -> OpenHarnessApiError:
    """Map any provider/transport exception onto the local error taxonomy.

    ``type_name`` lets adapters pass the SDK class name explicitly when the
    exception has been wrapped and ``type(exc).__name__`` is no longer useful.
    """
    if isinstance(exc, OpenHarnessApiError):
        return exc

    name = type_name or type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

    message = str(exc) or name

    if is_completion_token_limit_error(exc):
        return CompletionTokenLimitFailure(message, supported_limit=completion_token_limit(exc))
    if is_context_length_error(exc):
        return ContextLengthFailure(message)

    if status in (401,) or "Authentication" in name:
        return AuthenticationFailure(message)
    if status == 403 or "PermissionDenied" in name:
        return PermissionFailure(message)
    if status == 429 or "RateLimit" in name:
        return RateLimitFailure(message)
    if status in (503, 529) or "Overload" in name:
        return OverloadFailure(message)

    if is_transient(exc):
        return ConnectionFailure(message)

    # Last resort: in-stream error payloads from OpenAI-compatible servers have
    # no status code, so the only signal is the prose.  This lives here so the
    # three adapters do not each grow their own phrase list (ADR-0012 D6).
    lowered = message.lower()
    if any(term in lowered for term in _RETRYABLE_PROSE):
        failure = ConnectionFailure(message)
        failure.status_code = status if isinstance(status, int) else None
        return failure

    error = RequestFailure(message)
    error.status_code = status if isinstance(status, int) else None
    return error


def classify_message(message: str) -> OpenHarnessApiError:
    """Classify a provider failure that arrived as text rather than a response.

    Codex-style streams report failures inside the event payload
    (``response.failed`` / ``error``).  Going through the taxonomy here keeps
    "is this worth retrying?" in one place instead of in a per-adapter phrase
    list.
    """
    return classify_api_error(RequestFailure(message))


def is_retryable_error(exc: Exception, *, type_name: str | None = None) -> bool:
    """Single source of truth for "may this request be retried unchanged?".

    Backed by the ``retryable`` flag on the taxonomy: adapters no longer carry
    their own status-code sets, which had already drifted (529 was retryable on
    the Anthropic path and not on the OpenAI-compatible one).
    """
    return classify_api_error(exc, type_name=type_name).retryable
