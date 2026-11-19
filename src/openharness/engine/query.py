"""Core query entry point: the compatibility ``run_query`` shim.

Prompt assembly, image preprocessing and turn orchestration live in
:mod:`openharness.engine.step_support`, :mod:`openharness.engine.turn_executor`
and :mod:`openharness.engine.query_types`.  This module only keeps the public
entry point that swarm workers and third-party callers import, so that moving
the shared types into a leaf module did not break anyone's import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from openharness.domain.messages import ConversationMessage
from openharness.domain.stream_events import StreamEvent
from openharness.domain.usage import UsageSnapshot
from openharness.engine.query_types import (
    REACTIVE_COMPACT_STATUS_MESSAGE,
    AskUserPrompt,
    MaxTurnsExceeded,
    PermissionPrompt,
    QueryContext,
)

# Re-exported for callers that imported the error classifier from this module
# before it moved to ``step_support``.
from openharness.engine.step_support import _is_prompt_too_long_error
from openharness.engine.turn_executor import TurnExecutor

__all__ = [
    "REACTIVE_COMPACT_STATUS_MESSAGE",
    "AskUserPrompt",
    "MaxTurnsExceeded",
    "PermissionPrompt",
    "QueryContext",
    "run_query",
]


async def run_query(
    context: QueryContext,
    messages: list[ConversationMessage],
) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
    """Compatibility shim that delegates turn orchestration.

    The implementation lives in :class:`openharness.engine.turn_executor.TurnExecutor`;
    this function remains for swarm workers and third-party callers.
    """
    async for event, usage in TurnExecutor(context).execute(messages):
        yield event, usage
