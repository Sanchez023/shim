"""Service-backed implementation of the Codex subagent port."""

from __future__ import annotations

from openharness.runtime.seams.subagent import CodexSubagentResult
from openharness.services.backends.codex_app_server import (
    CodexAppServerSubagentProvider,
)
from openharness.services.subagent_provider import SubagentManager


class ServiceCodexSubagentPort:
    """Run one Codex delegation through the existing subagent manager."""

    async def run(
        self,
        task: str,
        *,
        cwd: str,
        model: str | None = None,
    ) -> CodexSubagentResult:
        provider = CodexAppServerSubagentProvider(model=model)
        manager = SubagentManager([provider], max_runs=1)
        try:
            run = await manager.start(task, cwd=cwd, provider_name="codex")
            result = await run.wait()
        finally:
            await manager.close()
        return CodexSubagentResult(
            output=result.output,
            stop_reason=result.stop_reason,
            diagnostic=result.diagnostic,
        )
