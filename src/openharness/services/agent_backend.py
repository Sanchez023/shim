"""Agent-runtime seam.

The default backend wraps :class:`QueryEngine`; alternative backends (for
example Codex app-server) implement the same contract and emit the same
``StreamEvent`` values to existing UI consumers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from openharness.domain.events import EventSequencer, EventSink, NullEventSink
from openharness.domain.messages import ConversationMessage
from openharness.domain.stream_events import AssistantTurnComplete, StreamEvent
from openharness.runtime.seams.core import SessionAppendPort


@dataclass(frozen=True)
class AgentCapabilities:
    """What an agent backend can do."""

    streaming: bool = True
    tools: bool = True
    resume: bool = True
    cancel: bool = True
    approvals: bool = False
    native_reasoning_state: bool = False


@runtime_checkable
class AgentBackend(Protocol):
    """Submit a turn and receive the shared stream-event vocabulary."""

    def capabilities(self) -> AgentCapabilities:
        """Return backend capabilities."""

    def submit(
        self,
        prompt: str | ConversationMessage,
        *,
        max_turns: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run one user turn."""

    def resume(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        """Continue an interrupted tool loop."""

    async def cancel(self) -> None:
        """Request cancellation of the active turn."""


class SessionRecordingBackend:
    """Wrap an external backend and record its messages/events.

    External runtimes such as Codex app-server own their internal loop, so the
    host only records the user message, the final assistant message, and turn
    boundaries.
    """

    def __init__(
        self,
        backend: AgentBackend,
        session: SessionAppendPort,
        *,
        event_sink: EventSink | None = None,
        event_sequencer: EventSequencer | None = None,
        session_id: str = "",
    ) -> None:
        self._backend = backend
        self._session = session
        self._event_sink = event_sink or NullEventSink()
        self._session_id = session_id or session.session_id
        self._event_sequencer = event_sequencer or EventSequencer(
            session_id=self._session_id
        )

    def capabilities(self) -> AgentCapabilities:
        return self._backend.capabilities()

    async def _emit(self, kind: str, payload: dict[str, Any], *, turn_id: str) -> None:
        await self._event_sink.emit(
            self._event_sequencer.next(kind, payload, turn_id=turn_id)
        )

    async def submit(
        self,
        prompt: str | ConversationMessage,
        *,
        max_turns: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        user_message = (
            prompt
            if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        self._session.append(user_message)
        turn_id = uuid4().hex
        await self._emit("turn.start", {"prompt": user_message.text}, turn_id=turn_id)
        await self._emit(
            "user.message",
            {"message": user_message.model_dump(mode="json")},
            turn_id=turn_id,
        )
        status = "completed"
        try:
            async for event in self._backend.submit(prompt, max_turns=max_turns):
                if isinstance(event, AssistantTurnComplete):
                    self._session.append(event.message)
                    await self._emit(
                        "assistant.message",
                        {
                            "message": event.message.model_dump(mode="json"),
                            "usage": event.usage.model_dump(mode="json"),
                        },
                        turn_id=turn_id,
                    )
                yield event
        except Exception:
            status = "error"
            raise
        finally:
            await self._emit("turn.end", {"status": status}, turn_id=turn_id)

    async def resume(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        async for event in self._backend.resume(max_turns=max_turns):
            yield event

    async def cancel(self) -> None:
        await self._backend.cancel()


class LocalPythonBackend:
    """Default backend backed by the in-process :class:`QueryEngine`."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            streaming=True,
            tools=True,
            resume=True,
            cancel=False,
            approvals=True,
            native_reasoning_state=True,
        )

    async def submit(
        self,
        prompt: str | ConversationMessage,
        *,
        max_turns: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        engine_max_turns = getattr(self._engine, "_max_turns", None)
        if max_turns is not None:
            self._engine.set_max_turns(max_turns)
        try:
            async for event in self._engine.submit_message(prompt):
                yield event
        finally:
            if max_turns is not None:
                self._engine.set_max_turns(engine_max_turns)

    async def resume(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        async for event in self._engine.continue_pending(max_turns=max_turns):
            yield event

    async def cancel(self) -> None:
        # The in-process loop is cancelled by closing the async generator that
        # owns it; there is no separate cancellation token to flip.
        return None
