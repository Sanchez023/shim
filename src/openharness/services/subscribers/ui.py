"""Subscriber that adapts canonical events to the legacy UI stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from openharness.domain.events import AgentEvent
from openharness.domain.messages import ConversationMessage, strip_private_blocks
from openharness.domain.stream_events import (
    AssistantTextDelta,
    AssistantThinkingDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.domain.usage import UsageSnapshot


class UIEventSubscriber:
    """Turn :class:`AgentEvent` values into the existing ``StreamEvent`` union.

    The UI runtime can keep consuming ``QueryEngine.submit_message()``; this
    subscriber exists so a future UI can consume the canonical stream directly
    without a second source of truth.
    """

    def __init__(self, *, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def on_event(self, event: AgentEvent) -> None:
        if self._closed:
            return
        mapped = self._map(event)
        if mapped is not None:
            await self._queue.put(mapped)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Yield mapped stream events until closed."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    @staticmethod
    def _map(event: AgentEvent) -> StreamEvent | None:
        payload = event.payload
        if event.kind == "assistant.text.delta":
            text = payload.get("text")
            return AssistantTextDelta(text=text) if isinstance(text, str) else None

        if event.kind == "assistant.thinking.delta":
            text = payload.get("text")
            return AssistantThinkingDelta(text=text) if isinstance(text, str) else None

        if event.kind == "assistant.message":
            raw_message = payload.get("message")
            if not isinstance(raw_message, dict):
                return None
            try:
                message = strip_private_blocks(
                    ConversationMessage.model_validate(raw_message)
                )
            except (ValueError, TypeError):
                return None
            raw_usage = payload.get("usage")
            usage = (
                UsageSnapshot.model_validate(raw_usage)
                if isinstance(raw_usage, dict)
                else UsageSnapshot()
            )
            return AssistantTurnComplete(message=message, usage=usage)

        if event.kind == "tool.started":
            raw_tool_input = payload.get("tool_input")
            tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
            return ToolExecutionStarted(
                tool_name=str(payload.get("tool_name") or ""),
                tool_input=tool_input,
                tool_use_id=str(payload.get("tool_use_id") or ""),
            )

        if event.kind == "tool.completed":
            metadata = payload.get("metadata")
            return ToolExecutionCompleted(
                tool_name=str(payload.get("tool_name") or ""),
                output=str(payload.get("output") or ""),
                is_error=bool(payload.get("is_error")),
                metadata=metadata if isinstance(metadata, dict) else None,
                tool_use_id=str(payload.get("tool_use_id") or ""),
            )

        if event.kind == "error":
            return ErrorEvent(
                message=str(payload.get("message") or ""),
                recoverable=bool(payload.get("recoverable", True)),
            )

        if event.kind == "status":
            return StatusEvent(message=str(payload.get("message") or ""))

        if event.kind == "compact.progress":
            phase = payload.get("phase")
            trigger = payload.get("trigger")
            if not isinstance(phase, str) or not isinstance(trigger, str):
                return None
            metadata = payload.get("metadata")
            return CompactProgressEvent(
                phase=cast(Any, phase),
                trigger=cast(Any, trigger),
                message=payload.get("message"),
                attempt=payload.get("attempt"),
                checkpoint=payload.get("checkpoint"),
                metadata=metadata if isinstance(metadata, dict) else None,
            )

        return None
