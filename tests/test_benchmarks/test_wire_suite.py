"""Keep the offline wire-layer suite in the normal CI test run.

Like the core suite, these cases are deterministic and need no network or API
keys; a regression in the provider boundary should fail here rather than only
when someone remembers to run the benchmark manually.
"""

from __future__ import annotations

import pytest

from benchmarks.spec import grade_run
from benchmarks.suites import WIRE_TASKS


@pytest.mark.asyncio
async def test_wire_benchmark_suite_passes():
    for task in WIRE_TASKS:
        assert task.runner is not None, f"{task.id} is missing its offline runner"
        run = grade_run(task, await task.runner(task))
        assert run.passed, f"{task.id} failed: {run.failures}"
