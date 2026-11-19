"""Execution seams: model calls, tool execution and human approval."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from openharness.api.capabilities import ModelCapabilities
from openharness.api.client import ApiMessageRequest, ApiStreamEvent
from openharness.domain.messages import ToolResultBlock


@runtime_checkable
class ModelClient(Protocol):
    """A streaming model adapter."""

    def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Return an async iterator that streams one response."""


def model_capabilities(client: object) -> ModelCapabilities:
    """Return declared capabilities, falling back to conservative defaults."""
    declared = getattr(client, "capabilities", None)
    if callable(declared):
        try:
            value = declared()
        except Exception:  # noqa: BLE001 - capabilities are advisory
            value = None
        if isinstance(value, ModelCapabilities):
            return value
    return ModelCapabilities(images=_supports_images(client))


def _supports_images(client: object) -> bool:
    return type(client).__name__ in {
        "AnthropicApiClient",
        "CodexApiClient",
        "OpenAICompatibleClient",
    }


@runtime_checkable
class ToolExecutor(Protocol):
    """Executes one tool call without owning the query loop."""

    async def execute(
        self,
        context: Any,
        tool_name: str,
        tool_use_id: str,
        tool_input: dict[str, object],
    ) -> ToolResultBlock:
        """Run one tool call."""


@runtime_checkable
class ApprovalGate(Protocol):
    """Asks a human (or policy) whether a gated action may proceed."""

    async def request(
        self,
        *,
        tool_name: str,
        reason: str,
        context: Any | None = None,
    ) -> bool:
        """Return True when the action is approved."""
