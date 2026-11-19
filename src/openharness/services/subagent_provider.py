"""Sub-agent provider seam and lifecycle manager."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubagentCapabilities:
    """Declared abilities of a sub-agent provider."""

    streaming: bool = False
    continuation: bool = False
    cancellation: bool = True
    external: bool = False


@dataclass(frozen=True)
class SubagentStartRequest:
    """One delegated task."""

    task: str
    cwd: str
    parent_session_id: str = ""
    model: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentResult:
    """Normalized terminal result for every provider."""

    output: str
    structured: dict[str, Any] | None = None
    diagnostic: str | None = None
    stop_reason: str = "completed"


@dataclass
class SubagentRun:
    """A live delegated task."""

    id: str
    provider_name: str
    task: str
    _runner: asyncio.Task[SubagentResult]

    async def wait(self) -> SubagentResult:
        return await self._runner

    def cancel(self) -> None:
        self._runner.cancel()

    @property
    def done(self) -> bool:
        return self._runner.done()


@runtime_checkable
class SubagentProvider(Protocol):
    """A replaceable sub-agent backend."""

    name: str
    inherits_parent_context: bool
    capabilities: SubagentCapabilities

    async def start(self, request: SubagentStartRequest) -> SubagentRun:
        """Start one delegated task."""


class LocalCallableSubagentProvider:
    """In-process provider backed by an async callable.

    This is the default provider for tests and for runtimes that already have
    an in-process agent factory.
    """

    def __init__(
        self,
        name: str,
        handler: Callable[[SubagentStartRequest], Awaitable[SubagentResult]],
        *,
        inherits_parent_context: bool = False,
        capabilities: SubagentCapabilities | None = None,
    ) -> None:
        self.name = name
        self._handler = handler
        self.inherits_parent_context = inherits_parent_context
        self.capabilities = capabilities or SubagentCapabilities()

    async def start(self, request: SubagentStartRequest) -> SubagentRun:
        async def _run() -> SubagentResult:
            return await self._handler(request)

        runner = asyncio.create_task(_run())
        return SubagentRun(
            id=uuid.uuid4().hex,
            provider_name=self.name,
            task=request.task,
            _runner=runner,
        )


class SubagentManager:
    """Own identity, capacity and teardown for delegated tasks."""

    def __init__(
        self,
        providers: list[SubagentProvider] | None = None,
        *,
        max_runs: int = 8,
    ) -> None:
        self._providers: dict[str, SubagentProvider] = {}
        self._runs: dict[str, SubagentRun] = {}
        self._completed: dict[str, SubagentResult] = {}
        self._max_runs = max(1, max_runs)
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: SubagentProvider) -> Callable[[], None]:
        if not provider.name:
            raise ValueError("subagent provider requires a name")
        previous = self._providers.get(provider.name)
        self._providers[provider.name] = provider

        def dispose() -> None:
            if self._providers.get(provider.name) is provider:
                if previous is None:
                    self._providers.pop(provider.name, None)
                else:
                    self._providers[provider.name] = previous

        return dispose

    @property
    def providers(self) -> dict[str, SubagentProvider]:
        return dict(self._providers)

    @property
    def runs(self) -> dict[str, SubagentRun]:
        return dict(self._runs)

    async def start(
        self,
        task: str,
        *,
        cwd: str,
        provider_name: str | None = None,
        parent_session_id: str = "",
        model: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> SubagentRun:
        if len(self._runs) >= self._max_runs:
            raise RuntimeError(
                f"subagent capacity exhausted ({self._max_runs} active runs)"
            )
        name = provider_name or next(iter(self._providers), "")
        if not name or name not in self._providers:
            raise KeyError(f"unknown subagent provider: {name!r}")
        provider = self._providers[name]
        request = SubagentStartRequest(
            task=task,
            cwd=cwd,
            parent_session_id=parent_session_id,
            model=model,
            config=dict(config or {}),
        )
        run = await provider.start(request)
        self._runs[run.id] = run

        def _on_done(task: asyncio.Task[SubagentResult]) -> None:
            self._runs.pop(run.id, None)
            if not task.cancelled() and task.exception() is None:
                self._completed[run.id] = task.result()

        run._runner.add_done_callback(_on_done)
        return run

    async def wait(self, run_id: str) -> SubagentResult:
        completed = self._completed.pop(run_id, None)
        if completed is not None:
            return completed
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown subagent run: {run_id}")
        try:
            return await run.wait()
        finally:
            self._runs.pop(run.id, None)

    def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown subagent run: {run_id}")
        run.cancel()

    async def close(self) -> None:
        for run in list(self._runs.values()):
            run.cancel()
        if self._runs:
            await asyncio.gather(
                *(run._runner for run in list(self._runs.values())),
                return_exceptions=True,
            )
        self._runs.clear()
