"""Tool abstractions."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from openharness.domain.messages import ImageBlock
    from openharness.hooks.executor import HookExecutor
    from openharness.runtime.seams.agents import AgentRunPort


@dataclass
class ToolExecutionContext:
    """Shared execution context for tool invocations."""

    cwd: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    hook_executor: HookExecutor | None = None
    agent_run_port: AgentRunPort | None = None
    #: Set when the user asks (Ctrl+B) for a long-running call to continue in
    #: the background.  Tools that cannot be detached simply ignore it.
    background_event: asyncio.Event | None = None
    #: Stream a preview of partial output to the UI while the tool is running.
    #: Tools that cannot stream simply ignore it.
    output_callback: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True)
class ToolResult:
    """Normalized tool execution result."""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Images the tool produced (screenshots, rendered charts, ...).  The engine
    #: forwards them to the model as image content blocks; tools that only
    #: return text leave this empty.
    images: list[ImageBlock] = field(default_factory=list)


ParamsT = TypeVar("ParamsT", bound=BaseModel)


class BaseTool(ABC, Generic[ParamsT]):
    """Base class for all OpenHarness tools.

    ``ParamsT`` is the tool's own ``input_model``.  Parameterising the base
    lets each subclass narrow ``execute``/``is_read_only`` to its real argument
    model instead of re-checking ``isinstance`` after the dispatch layer has
    already validated it.
    """

    name: str
    description: str
    input_model: type[ParamsT]

    @abstractmethod
    async def execute(self, arguments: ParamsT, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool."""

    def is_read_only(self, arguments: ParamsT) -> bool:
        """Return whether the invocation is read-only."""
        del arguments
        return False

    def read_only_verdict(self, arguments: ParamsT) -> str | None:
        """Return ``"read_only"``/``"mutating"``/``"unknown"`` when known.

        Most tools are statically one or the other, so ``None`` means "no finer
        information than :meth:`is_read_only`".  Tools whose read-onlyness
        depends on their arguments (``bash``) override this so a caller can
        distinguish "will mutate" from "cannot tell" (ADR-0013 D2).
        """
        del arguments
        return None

    def read_only_reason(self, arguments: ParamsT) -> str | None:
        """Return a short machine-readable reason for :meth:`read_only_verdict`.

        Surfaced in the ``permission.decided`` event so blocked constructs can be
        counted (``xargs`` vs ``command_substitution`` vs ``write_flag``) instead
        of guessed at from user reports (ADR-0013 D3).
        """
        del arguments
        return None

    def to_api_schema(self) -> dict[str, Any]:
        """Return the tool schema expected by the Anthropic Messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    """Map tool names to implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool[Any]] = {}

    def register(self, tool: BaseTool[Any]) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool[Any] | None:
        """Return a registered tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool[Any]]:
        """Return all registered tools."""
        return list(self._tools.values())

    def to_api_schema(
        self,
        tool_names: Iterable[str] | None = None,
        *,
        append_order: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return tool schemas in API format, optionally filtered by name.

        ``append_order`` moves the named tools to the end of the list in the
        given order while keeping every other tool in registration order.  The
        query engine uses it for deferred tools activated by ``tool_search``:
        appending preserves the provider's cached tool prefix, whereas
        inserting a tool at its registration position would invalidate every
        schema after that point.
        """
        names = None if tool_names is None else set(tool_names)
        visible = [
            tool
            for name, tool in self._tools.items()
            if names is None or name in names
        ]
        if append_order is None:
            return [tool.to_api_schema() for tool in visible]

        ordered_names = list(
            dict.fromkeys(
                name
                for name in append_order
                if name in self._tools and (names is None or name in names)
            )
        )
        appended = set(ordered_names)
        result = [tool.to_api_schema() for tool in visible if tool.name not in appended]
        result.extend(self._tools[name].to_api_schema() for name in ordered_names)
        return result
