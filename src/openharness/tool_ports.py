"""Default port providers for direct model-tool calls.

Tools receive concrete ports through ``ToolExecutionContext`` metadata in the
assembled runtime. This module is the composition fallback: it maps the pure
protocols in ``runtime.seams`` to their service implementations without
putting service imports into the seams package.
"""

from __future__ import annotations

from openharness.runtime.seams.agents import AgentRunPort
from openharness.runtime.seams.cron import CronPort
from openharness.runtime.seams.lsp import LspPort
from openharness.runtime.seams.subagent import CodexSubagentPort


def get_default_agent_run_port() -> AgentRunPort:
    from openharness.services.agent_run_port import ServiceAgentRunPort

    return ServiceAgentRunPort()


def get_default_cron_port() -> CronPort:
    from openharness.services.cron_port import ServiceCronPort

    return ServiceCronPort()


def get_default_lsp_port() -> LspPort:
    from openharness.services.lsp_port import ServiceLspPort

    return ServiceLspPort()


def get_default_codex_subagent_port() -> CodexSubagentPort:
    from openharness.services.subagent_port import ServiceCodexSubagentPort

    return ServiceCodexSubagentPort()


__all__ = [
    "get_default_agent_run_port",
    "get_default_codex_subagent_port",
    "get_default_cron_port",
    "get_default_lsp_port",
]
