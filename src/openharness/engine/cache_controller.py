"""Prompt-cache lifecycle: warmup, keepalive and attribution counters.

Extracted verbatim from :class:`openharness.engine.query_engine.QueryEngine`,
which had grown to own the conversation, the tool visibility set, the prompt
and the cache machinery at once.  This controller is deliberately narrow:

* it never mutates the conversation, the tool set or the prompt;
* it reads the *live* prompt and tool schemas through :class:`CacheSource` at
  the moment a warmup request is built, so a ``/model`` or tool activation is
  picked up immediately.  Copying the prefix into the controller would keep
  warming a stale prefix and silently cost the provider-cache hits.

Cache-friendly behaviour here is load-bearing (ADR-0008): the request built in
:meth:`CacheController.warmup_cache` must stay byte-identical to the prefix a
real turn sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Protocol

from openharness.api.client import ApiMessageRequest
from openharness.engine.cache_diagnostics import CacheDiagnostics
from openharness.engine.cache_warmup import CacheKeepalive, CacheWarmup


class CacheSource(Protocol):
    """The slice of the engine that cache management is allowed to read."""

    @property
    def api_client(self) -> Any: ...

    @property
    def model(self) -> str: ...

    @property
    def effort(self) -> str | None: ...

    @property
    def system_prompt(self) -> str: ...

    @property
    def system_prompt_cacheable_prefix(self) -> str: ...

    def visible_tool_schemas(self) -> list[dict[str, Any]]: ...


class CacheController:
    """Own the warmup task, the keepalive loop and the cache statistics."""

    def __init__(self, source: CacheSource, *, warmup_wait_seconds: float = 2.0) -> None:
        self._source = source
        self._warmup_wait_seconds = max(0.0, float(warmup_wait_seconds))
        self.diagnostics = CacheDiagnostics()
        self._warmup: CacheWarmup | None = None
        self._warmup_task: asyncio.Task[None] | None = None
        self._warmup_waited = False
        self._keepalive: CacheKeepalive | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_model_request_at = 0.0
        self._model_requests_in_flight = 0

    @property
    def cache_summary(self) -> dict[str, Any]:
        """Return overall, hot-request and drift counters for /usage."""
        return self.diagnostics.summary()

    def set_cache_warmup(self, warmup: CacheWarmup | None) -> None:
        """Attach a warmup controller for implicit-cache providers."""
        self.stop_keepalive()
        self._warmup = warmup
        self._warmup_task = None
        self._warmup_waited = False

    def set_warmup_task(self, task: asyncio.Task[None]) -> None:
        """Track the background warmup so the first turn can briefly wait."""
        self._warmup_task = task
        self._warmup_waited = False

    async def wait_for_warmup(self) -> None:
        """Wait briefly for the startup warmup on the first turn.

        Interactive users naturally pause before typing, so the background
        task usually finishes first.  Headless ``-p`` runs submit immediately;
        a short wait keeps the first request from racing the cache write.
        """
        if self._warmup_waited or self._warmup_task is None:
            return
        self._warmup_waited = True
        if self._warmup_task.done():
            with contextlib.suppress(Exception):
                self._warmup_task.result()
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.shield(self._warmup_task),
                timeout=self._warmup_wait_seconds,
            )

    @property
    def warmup_stats(self) -> dict[str, int] | None:
        """Return warmup accounting, kept separate from real request usage."""
        return self._warmup.stats.as_dict() if self._warmup else None

    async def warmup_cache(self, *, force: bool = False) -> None:
        """Write the current system+tools prefix into the provider cache.

        Runs in the background at session start.  Failures are logged by the
        controller and never propagate to the turn loop.  ``force`` bypasses
        the warmup TTL dedupe and is used by the keepalive.
        """
        if self._warmup is None:
            return
        source = self._source
        tools = source.visible_tool_schemas()
        key = self._warmup.key(
            base_url=getattr(source.api_client, "base_url", None),
            model=source.model,
            system_prompt=source.system_prompt,
            tools=tools,
        )
        request = ApiMessageRequest(
            model=source.model,
            messages=[],
            system_prompt=source.system_prompt,
            system_prompt_cacheable_prefix=source.system_prompt_cacheable_prefix,
            max_tokens=1,
            tools=tools,
            effort=source.effort,
        )
        # A warmup is a model request too: do not let the keepalive fire again
        # immediately after a refresh, even if the controller skipped it.
        self._last_model_request_at = time.monotonic()
        await self._warmup.warm(source.api_client, request, key=key, force=force)

    def note_model_request_start(self) -> None:
        """Record that a real model request is in flight."""
        self._model_requests_in_flight += 1
        self._last_model_request_at = time.monotonic()

    def note_model_request_end(self) -> None:
        """Record that a real model request finished (or failed)."""
        self._model_requests_in_flight = max(0, self._model_requests_in_flight - 1)
        self._last_model_request_at = time.monotonic()

    @property
    def last_model_request_at(self) -> float:
        """Return the monotonic time of the last model request, or 0."""
        return self._last_model_request_at

    def start_keepalive(self, idle_seconds: float = 10 * 60) -> asyncio.Task[None] | None:
        """Start the opt-in prefix keepalive and return its task.

        Returns ``None`` when no warmup controller is attached, because there
        is nothing to refresh in that case.
        """
        if self._warmup is None:
            return None
        self.stop_keepalive()
        keepalive = CacheKeepalive(idle_seconds=idle_seconds)
        self._keepalive = keepalive
        task = keepalive.start(
            refresh=self._refresh_prefix,
            last_request_at=lambda: self._last_model_request_at,
            request_in_flight=lambda: self._model_requests_in_flight > 0,
        )
        self._keepalive_task = task
        return task

    def stop_keepalive(self) -> asyncio.Task[None] | None:
        """Cancel the keepalive task and return it for optional awaiting."""
        task = self._keepalive_task
        self._keepalive_task = None
        self._keepalive = None
        if task is not None and not task.done():
            task.cancel()
            return task
        return None

    @property
    def keepalive_stats(self) -> dict[str, float] | None:
        """Return keepalive counters, or ``None`` when it is not running."""
        if self._keepalive is None:
            return None
        return self._keepalive.stats()

    async def _refresh_prefix(self) -> None:
        """Warm the prefix on behalf of the keepalive loop."""
        try:
            await self.warmup_cache(force=True)
        finally:
            self._last_model_request_at = time.monotonic()
