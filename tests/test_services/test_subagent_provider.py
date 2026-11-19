from __future__ import annotations

import asyncio

import pytest

from openharness.services.subagent_provider import (
    LocalCallableSubagentProvider,
    SubagentManager,
    SubagentResult,
    SubagentStartRequest,
)


@pytest.mark.asyncio
async def test_subagent_manager_runs_and_normalizes_result():
    async def handler(request: SubagentStartRequest) -> SubagentResult:
        return SubagentResult(output=f"handled: {request.task}")

    manager = SubagentManager(
        [LocalCallableSubagentProvider("local", handler)]
    )
    run = await manager.start("hello", cwd="/tmp")

    result = await manager.wait(run.id)

    assert result.output == "handled: hello"
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_subagent_manager_registration_is_disposable():
    async def handler(request: SubagentStartRequest) -> SubagentResult:
        return SubagentResult(output=request.task)

    manager = SubagentManager()
    dispose = manager.register(LocalCallableSubagentProvider("local", handler))

    assert "local" in manager.providers
    dispose()
    assert "local" not in manager.providers


@pytest.mark.asyncio
async def test_subagent_manager_enforces_capacity():
    async def handler(request: SubagentStartRequest) -> SubagentResult:
        await asyncio.sleep(0.05)
        return SubagentResult(output=request.task)

    manager = SubagentManager(
        [LocalCallableSubagentProvider("local", handler)], max_runs=1
    )
    await manager.start("first", cwd="/tmp")

    with pytest.raises(RuntimeError):
        await manager.start("second", cwd="/tmp")
    await manager.close()


@pytest.mark.asyncio
async def test_subagent_manager_cancels_runs():
    async def handler(request: SubagentStartRequest) -> SubagentResult:
        await asyncio.sleep(5)
        return SubagentResult(output=request.task)

    manager = SubagentManager(
        [LocalCallableSubagentProvider("local", handler)]
    )
    run = await manager.start("slow", cwd="/tmp")
    manager.cancel(run.id)

    with pytest.raises(asyncio.CancelledError):
        await run.wait()
    await manager.close()
