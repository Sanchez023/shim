"""Keep the in-repo benchmark suite honest under CI.

The deterministic suite is fast (a few seconds) and needs no API access, so a
regression there should fail the normal test run rather than wait for someone
to invoke the benchmark manually.
"""

from __future__ import annotations

import pytest

from benchmarks.spec import run_task
from benchmarks.suites import CORE_TASKS


@pytest.mark.asyncio
async def test_core_benchmark_suite_passes():
    for task in CORE_TASKS:
        run = await run_task(task, scripted=True)
        assert run.passed, f"{task.id} failed: {run.failures}"
