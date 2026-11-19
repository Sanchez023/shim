"""Warmup controller accounting and dedupe."""

from __future__ import annotations

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.cache_warmup import CacheKeepalive, CacheWarmup


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def warmup(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return UsageSnapshot(input_tokens=5_800, output_tokens=1)


def _key(warmup: CacheWarmup) -> str:
    return warmup.key(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        system_prompt="SYSTEM",
        tools=[{"name": "read_file"}],
    )


@pytest.mark.asyncio
async def test_warmup_runs_once_per_ttl_window():
    warmup = CacheWarmup(ttl_seconds=300)
    client = _Client()
    key = _key(warmup)

    first = await warmup.warm(client, object(), key=key)
    second = await warmup.warm(client, object(), key=key)

    assert first is not None
    assert second is None
    assert client.calls == 1
    assert warmup.stats.attempts == 1
    assert warmup.stats.skipped == 1
    assert warmup.stats.input_tokens == 5_800
    assert warmup.stats.output_tokens == 1


@pytest.mark.asyncio
async def test_failed_warmup_is_swallowed_and_not_retried_immediately():
    warmup = CacheWarmup(ttl_seconds=300)
    client = _Client(fail=True)
    key = _key(warmup)

    assert await warmup.warm(client, object(), key=key) is None
    assert await warmup.warm(client, object(), key=key) is None

    assert client.calls == 1
    assert warmup.stats.failures == 1
    assert warmup.stats.skipped == 1


@pytest.mark.asyncio
async def test_client_without_warmup_method_is_skipped():
    warmup = CacheWarmup()

    assert await warmup.warm(object(), object(), key="k") is None
    assert warmup.stats.skipped == 1
    assert warmup.stats.attempts == 0


def test_key_changes_with_the_prefix():
    warmup = CacheWarmup()
    base = {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "system_prompt": "SYSTEM",
        "tools": [{"name": "read_file"}],
    }

    assert warmup.key(**base) != warmup.key(**{**base, "system_prompt": "OTHER"})
    assert warmup.key(**base) != warmup.key(**{**base, "model": "deepseek-flash"})


@pytest.mark.asyncio
async def test_wait_ready_waits_for_an_inflight_warmup():
    import asyncio

    class _SlowClient:
        async def warmup(self, request):
            await asyncio.sleep(0.05)
            return UsageSnapshot(input_tokens=10, output_tokens=1)

    warmup = CacheWarmup(ttl_seconds=300, timeout_seconds=5)
    task = asyncio.create_task(warmup.warm(_SlowClient(), object(), key="slow"))
    await asyncio.sleep(0)

    assert await warmup.wait_ready(1.0) is True
    await task


@pytest.mark.asyncio
async def test_wait_ready_times_out_without_cancelling_the_warmup():
    import asyncio

    class _VerySlowClient:
        async def warmup(self, request):
            await asyncio.sleep(0.2)
            return UsageSnapshot(input_tokens=10, output_tokens=1)

    warmup = CacheWarmup(ttl_seconds=300, timeout_seconds=5)
    task = asyncio.create_task(warmup.warm(_VerySlowClient(), object(), key="slow"))
    await asyncio.sleep(0)

    assert await warmup.wait_ready(0.01) is False
    assert not task.done()
    await task


@pytest.mark.asyncio
async def test_force_bypasses_the_ttl_dedupe():
    warmup = CacheWarmup(ttl_seconds=300)
    client = _Client()
    key = _key(warmup)

    await warmup.warm(client, object(), key=key)
    await warmup.warm(client, object(), key=key, force=True)

    assert client.calls == 2
    assert warmup.stats.attempts == 2


@pytest.mark.asyncio
async def test_keepalive_refreshes_after_the_idle_window():
    import asyncio
    import time

    calls: list[float] = []
    last = {"at": time.monotonic()}

    async def refresh() -> None:
        calls.append(time.monotonic())
        # The engine updates the timestamp on every refresh; emulate that so
        # the loop waits another full idle window instead of spinning.
        last["at"] = time.monotonic()

    keepalive = CacheKeepalive(idle_seconds=0.02, check_seconds=0.01)
    task = keepalive.start(
        refresh=refresh,
        last_request_at=lambda: last["at"],
        request_in_flight=lambda: False,
    )

    async def _wait() -> None:
        while len(calls) < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=2.0)
    await keepalive.stop()

    assert task.done()
    assert keepalive.refreshes >= 2
    assert keepalive.failures == 0


@pytest.mark.asyncio
async def test_keepalive_does_not_refresh_while_a_request_is_in_flight():
    import asyncio

    calls: list[int] = []

    async def refresh() -> None:
        calls.append(1)

    keepalive = CacheKeepalive(idle_seconds=0.01, check_seconds=0.01)
    keepalive.start(
        refresh=refresh,
        last_request_at=lambda: 0.0,
        request_in_flight=lambda: True,
    )
    await asyncio.sleep(0.08)
    await keepalive.stop()

    assert calls == []
    assert keepalive.skipped_busy >= 1


@pytest.mark.asyncio
async def test_keepalive_waits_for_the_idle_window():
    import asyncio
    import time

    calls: list[int] = []

    async def refresh() -> None:
        calls.append(1)

    keepalive = CacheKeepalive(idle_seconds=10, check_seconds=0.01)
    keepalive.start(
        refresh=refresh,
        last_request_at=time.monotonic,
        request_in_flight=lambda: False,
    )
    await asyncio.sleep(0.05)
    await keepalive.stop()

    assert calls == []


@pytest.mark.asyncio
async def test_keepalive_counts_failures_without_dying():
    import asyncio
    import time

    attempts: list[int] = []
    last = {"at": time.monotonic()}

    async def refresh() -> None:
        attempts.append(1)
        last["at"] = time.monotonic()
        raise RuntimeError("provider down")

    keepalive = CacheKeepalive(idle_seconds=0.02, check_seconds=0.01)
    keepalive.start(
        refresh=refresh,
        last_request_at=lambda: last["at"],
        request_in_flight=lambda: False,
    )
    await asyncio.sleep(0.12)
    await keepalive.stop()

    assert attempts
    assert keepalive.failures == len(attempts)


@pytest.mark.asyncio
async def test_engine_keepalive_uses_forced_warmup(tmp_path):
    from openharness.config.settings import PermissionSettings
    from openharness.engine.query_engine import QueryEngine
    from openharness.permissions import PermissionChecker
    from openharness.tools import create_default_tool_registry

    class _Warmup:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def key(self, **_: object) -> str:
            return "k"

        async def warm(self, client, request, *, key, force=False):
            self.calls.append(force)

    engine = QueryEngine(
        api_client=object(),  # type: ignore[arg-type]
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt="STABLE",
        system_prompt_cacheable_prefix="STABLE",
    )
    warmup = _Warmup()
    engine.set_cache_warmup(warmup)  # type: ignore[arg-type]
    engine.note_model_request_start()
    engine.note_model_request_end()

    await engine.warmup_cache(force=True)
    assert warmup.calls == [True]

    import asyncio

    task = engine.start_cache_refresh(0.02)
    assert task is not None

    async def _wait() -> None:
        while len(warmup.calls) < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=2.0)
    engine.stop_cache_refresh()
    await asyncio.sleep(0)
    assert task.done()
    assert warmup.calls[-1] is True
