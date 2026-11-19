"""Best-effort prompt-cache warmup for implicit-cache providers.

Warming is deliberately narrow: one non-streaming request per provider/model
prefix per TTL window, fired in the background at session startup.  It does not
reduce the total token bill on its own (the prefix is still paid once as a
miss), but it moves that miss off the first real request and lets later
sessions with the same prefix reuse it.  Idle-session pings are not supported:
the provider TTL is not public and repeated pings cost more than they save.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openharness.domain.usage import UsageSnapshot

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_REFRESH_IDLE_SECONDS = 10 * 60


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class WarmupStats:
    """Accounting for warmup requests, kept out of the user-visible hit rate."""

    attempts: int = 0
    skipped: int = 0
    failures: int = 0
    input_tokens: int = 0
    cache_hit_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage: UsageSnapshot) -> None:
        self.input_tokens += usage.input_tokens + usage.cache_creation_input_tokens
        self.cache_hit_tokens += usage.cache_read_input_tokens
        self.output_tokens += usage.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "skipped": self.skipped,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "output_tokens": self.output_tokens,
        }


class CacheWarmup:
    """Warm one provider prefix per TTL window."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._timeout = max(0.1, float(timeout_seconds))
        self._last: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._ready = asyncio.Event()
        self._ready.set()
        self.stats = WarmupStats()

    @property
    def ttl_seconds(self) -> float:
        """Return the dedupe window in seconds."""
        return self._ttl

    @staticmethod
    def key(
        *,
        base_url: str | None,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> str:
        """Return the dedupe key for one provider prefix."""
        tools_json = json.dumps(
            tools, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return _sha(f"{base_url or ''}|{model}|{system_prompt}|{tools_json}")

    def should_warm(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last = self._last.get(key)
        return last is None or current - last >= self._ttl

    async def warm(
        self,
        client: Any,
        request: Any,
        *,
        key: str,
        force: bool = False,
    ) -> UsageSnapshot | None:
        """Send one warmup request if the prefix is not already warm.

        ``force`` bypasses the TTL dedupe for the keepalive path, which exists
        precisely to refresh a prefix that is about to expire.
        """
        if key in self._inflight or (not force and not self.should_warm(key)):
            self.stats.skipped += 1
            return None
        warmup = getattr(client, "warmup", None)
        if not callable(warmup):
            self.stats.skipped += 1
            return None

        self._inflight.add(key)
        self._ready.clear()
        # Record the attempt before awaiting so a provider outage does not
        # turn every turn into another warmup retry.
        self._last[key] = time.monotonic()
        self.stats.attempts += 1
        try:
            usage = await asyncio.wait_for(warmup(request), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - warmup is best effort
            self.stats.failures += 1
            log.info("Prompt cache warmup failed (non-fatal): %s", exc)
            return None
        finally:
            self._inflight.discard(key)
            if not self._inflight:
                self._ready.set()

        if isinstance(usage, UsageSnapshot):
            self.stats.add(usage)
            return usage
        return None

    async def wait_ready(self, timeout: float) -> bool:
        """Wait for an in-flight warmup to finish, up to ``timeout`` seconds."""
        if timeout <= 0:
            return self._ready.is_set()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def reset(self) -> None:
        self._last.clear()
        self._inflight.clear()
        self._ready.set()
        self.stats = WarmupStats()


class CacheKeepalive:
    """Refresh a cached prefix during idle or long-tool gaps.

    Implicit provider caches expire after an undisclosed interval.  A session
    that spends ten minutes in a build tool, or simply sits idle, can lose the
    prefix before its next model request.  This controller watches the time of
    the last model request and re-warms the prefix when it has been idle for
    ``idle_seconds``.

    It is opt-in (``OPENHARNESS_CACHE_REFRESH=1``) because it spends tokens on
    a schedule and the provider TTL is not public.  It never races a real
    request: while one is in flight, the tick is skipped.
    """

    def __init__(
        self,
        *,
        idle_seconds: float = DEFAULT_REFRESH_IDLE_SECONDS,
        check_seconds: float | None = None,
    ) -> None:
        self._idle = max(0.01, float(idle_seconds))
        default_check = min(60.0, max(0.01, self._idle / 4.0))
        self._check = max(
            0.01, float(check_seconds) if check_seconds is not None else default_check
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.refreshes = 0
        self.failures = 0
        self.skipped_busy = 0

    @property
    def idle_seconds(self) -> float:
        """Return how long the prefix may sit idle before a refresh."""
        return self._idle

    def start(
        self,
        *,
        refresh: Callable[[], Awaitable[None]],
        last_request_at: Callable[[], float],
        request_in_flight: Callable[[], bool],
    ) -> asyncio.Task[None]:
        """Start the background loop and return its task."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(
                refresh=refresh,
                last_request_at=last_request_at,
                request_in_flight=request_in_flight,
            ),
            name="openharness-cache-keepalive",
        )
        return self._task

    async def stop(self) -> None:
        """Stop the loop and wait for its task to finish."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def stats(self) -> dict[str, float]:
        """Return keepalive counters for ``/usage``."""
        return {
            "refreshes": self.refreshes,
            "failures": self.failures,
            "skipped_busy": self.skipped_busy,
            "idle_seconds": self._idle,
        }

    async def _run(
        self,
        *,
        refresh: Callable[[], Awaitable[None]],
        last_request_at: Callable[[], float],
        request_in_flight: Callable[[], bool],
    ) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._check)
                return
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            if request_in_flight():
                # A real request is already refreshing the provider's entry.
                self.skipped_busy += 1
                continue
            last = last_request_at()
            if last <= 0.0 or time.monotonic() - last < self._idle:
                continue
            self.refreshes += 1
            try:
                await refresh()
            except Exception:
                self.failures += 1
                log.info("Cache keepalive refresh failed", exc_info=True)


_DEFAULT_WARMUP: CacheWarmup | None = None


def get_default_cache_warmup(ttl_seconds: float = DEFAULT_TTL_SECONDS) -> CacheWarmup:
    """Return the process-wide warmup controller.

    Sharing one controller across runtime bundles lets a prefix warmed by one
    short session be reused by the next session in the same process, which is
    the only case where warmup can reduce total cost.
    """
    global _DEFAULT_WARMUP
    if _DEFAULT_WARMUP is None or _DEFAULT_WARMUP.ttl_seconds != max(
        0.0, float(ttl_seconds)
    ):
        _DEFAULT_WARMUP = CacheWarmup(ttl_seconds=ttl_seconds)
    return _DEFAULT_WARMUP


def reset_default_cache_warmup() -> None:
    """Drop the process-wide controller (used by tests and session resets)."""
    global _DEFAULT_WARMUP
    _DEFAULT_WARMUP = None
