"""Turn-level orchestration: compaction, retries, and step sequencing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Literal
from uuid import uuid4

from openharness.domain.messages import ConversationMessage
from openharness.domain.stream_events import (
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
)
from openharness.domain.usage import UsageSnapshot
from openharness.engine.event_emit import emit_event as _emit_event
from openharness.engine.query_types import (
    REACTIVE_COMPACT_STATUS_MESSAGE,
    MaxTurnsExceeded,
    QueryContext,
)
from openharness.engine.step_runner import StepRunner
from openharness.engine.step_support import (
    _bounded_completion_tokens,
    _extract_completion_token_limit,
    _is_completion_token_limit_error,
    _is_prompt_too_long_error,
    _serialize_messages,
)

log = logging.getLogger(__name__)


def _take_pending_user_messages(context: QueryContext) -> list[ConversationMessage]:
    """Drain messages the user queued while this turn was already running."""
    drain = context.drain_pending_user_messages
    if drain is None:
        return []
    return drain()


class TurnExecutor:
    """Execute one turn as a sequence of :class:`StepRunner` steps."""

    def __init__(self, context: QueryContext) -> None:
        self._context: QueryContext = context
        self._compact_state: Any = None
        self._reactive_compact_attempted = False
        self._last_compaction_result: tuple[list[ConversationMessage], bool] = ([], False)
        self._effective_max_tokens = 0
        self._reported_token_clamp = False

    async def execute(
        self,
        messages: list[ConversationMessage],
    ) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
        context = self._context
        self._compact_state = (
            context.compact_port.new_state()
            if context.compact_port is not None
            else None
        )
        self._reactive_compact_attempted = False
        self._last_compaction_result = (messages, False)
        self._effective_max_tokens = _bounded_completion_tokens(
            context.max_tokens,
            context.context_window_tokens,
        )
        self._reported_token_clamp = False
        turn_count = 0

        while context.max_turns is None or turn_count < context.max_turns:
            turn_count += 1
            step_id = uuid4().hex
            context.current_step_id = step_id
            # Follow-ups typed while the turn was streaming join the
            # conversation here — after the previous step's tool results were
            # appended, before the next model request sees the history.
            for pending_message in _take_pending_user_messages(context):
                messages.append(pending_message)
                await _emit_event(
                    context,
                    "user.message",
                    {"message": pending_message.model_dump(mode="json")},
                    step_id=step_id,
                )
            await _emit_event(
                context,
                "step.start",
                {"turn": turn_count, "model": context.model},
                step_id=step_id,
            )

            if (
                self._effective_max_tokens != context.max_tokens
                and not self._reported_token_clamp
            ):
                self._reported_token_clamp = True
                status_message = (
                    "Requested max_tokens="
                    f"{context.max_tokens} exceeds the safe per-request output cap; "
                    f"using {self._effective_max_tokens}."
                )
                await _emit_event(
                    context,
                    "status",
                    {"message": status_message},
                    step_id=step_id,
                )
                yield StatusEvent(message=status_message), None

            # --- context governance / auto-compaction -----------------------
            governor_plan = None
            if context.context_governor is not None:
                governor_plan = await context.context_governor.on_step_pre(
                    list(messages),
                    model=context.model,
                    context_window_tokens=context.context_window_tokens,
                    auto_compact_threshold_tokens=context.auto_compact_threshold_tokens,
                )
                if governor_plan is not None and governor_plan.messages is not None:
                    messages[:] = governor_plan.messages
                    await _emit_event(
                        context,
                        "context.compacted",
                        {
                            "trigger": "governor",
                            "reason": governor_plan.reason,
                            "messages": _serialize_messages(messages),
                        },
                        step_id=step_id,
                    )
            if governor_plan is None or not governor_plan.skip_compaction:
                async for event, usage in self._stream_compaction(
                    messages, trigger="auto"
                ):
                    yield event, usage
                compacted_messages, was_compacted = self._last_compaction_result
                if compacted_messages is not messages:
                    messages[:] = compacted_messages
                if was_compacted:
                    await _emit_event(
                        context,
                        "context.compacted",
                        {"trigger": "auto", "messages": _serialize_messages(messages)},
                        step_id=step_id,
                    )
            # -----------------------------------------------------------------

            runner = StepRunner(context)
            try:
                async for event, usage in runner.run(
                    messages,
                    step_id=step_id,
                    max_tokens=self._effective_max_tokens,
                    turn_count=turn_count,
                ):
                    yield event, usage
            except MaxTurnsExceeded:
                raise
            except Exception as exc:
                error_msg = str(exc)
                if isinstance(exc, RuntimeError) and "without a final message" in error_msg:
                    raise

                if _is_completion_token_limit_error(exc):
                    supported_limit = _extract_completion_token_limit(exc)
                    if (
                        supported_limit is not None
                        and self._effective_max_tokens > supported_limit
                    ):
                        previous_max_tokens = self._effective_max_tokens
                        self._effective_max_tokens = supported_limit
                        status_message = (
                            f"Model rejected max_tokens={previous_max_tokens}; "
                            f"retrying with provider limit {self._effective_max_tokens}."
                        )
                        await _emit_event(
                            context,
                            "status",
                            {"message": status_message},
                            step_id=step_id,
                        )
                        yield StatusEvent(message=status_message), None
                        await _emit_event(
                            context,
                            "step.end",
                            {"status": "retry_token_limit"},
                            step_id=step_id,
                        )
                        turn_count = max(0, turn_count - 1)
                        continue

                if (
                    not self._reactive_compact_attempted
                    and _is_prompt_too_long_error(exc)
                ):
                    governor_plan = None
                    if context.context_governor is not None:
                        governor_plan = await context.context_governor.on_request_error(
                            list(messages), exc
                        )
                        if (
                            governor_plan is not None
                            and governor_plan.messages is not None
                        ):
                            messages[:] = governor_plan.messages
                            self._reactive_compact_attempted = True
                            await _emit_event(
                                context,
                                "context.compacted",
                                {
                                    "trigger": "governor",
                                    "reason": governor_plan.reason,
                                    "messages": _serialize_messages(messages),
                                },
                                step_id=step_id,
                            )
                            await _emit_event(
                                context,
                                "step.end",
                                {"status": "governor_compacted"},
                                step_id=step_id,
                            )
                            continue
                    if governor_plan is None or not governor_plan.skip_compaction:
                        self._reactive_compact_attempted = True
                        await _emit_event(
                            context,
                            "status",
                            {"message": REACTIVE_COMPACT_STATUS_MESSAGE},
                            step_id=step_id,
                        )
                        yield StatusEvent(message=REACTIVE_COMPACT_STATUS_MESSAGE), None
                        async for event, usage in self._stream_compaction(
                            messages, trigger="reactive", force=True
                        ):
                            yield event, usage
                        compacted_messages, was_compacted = self._last_compaction_result
                        if compacted_messages is not messages:
                            messages[:] = compacted_messages
                        if was_compacted:
                            await _emit_event(
                                context,
                                "context.compacted",
                                {
                                    "trigger": "reactive",
                                    "messages": _serialize_messages(messages),
                                },
                                step_id=step_id,
                            )
                            await _emit_event(
                                context,
                                "step.end",
                                {"status": "reactive_compact"},
                                step_id=step_id,
                            )
                            continue

                if (
                    "connect" in error_msg.lower()
                    or "timeout" in error_msg.lower()
                    or "network" in error_msg.lower()
                ):
                    error_message = (
                        f"Network error: {error_msg}. "
                        "Check your internet connection and try again."
                    )
                else:
                    error_message = f"API error: {error_msg}"
                await _emit_event(
                    context,
                    "error",
                    {"message": error_message, "recoverable": True},
                    step_id=step_id,
                )
                await _emit_event(
                    context,
                    "step.end",
                    {"status": "error"},
                    step_id=step_id,
                )
                yield ErrorEvent(message=error_message), None
                return

            if not runner.outcome.needs_followup:
                return

        if context.max_turns is not None:
            message = f"Exceeded maximum turn limit ({context.max_turns})"
            await _emit_event(context, "error", {"message": message, "recoverable": False})
            raise MaxTurnsExceeded(context.max_turns)
        raise RuntimeError("Query loop exited without a max_turns limit or final response")

    async def _stream_compaction(
        self,
        messages: list[ConversationMessage],
        *,
        trigger: Literal["auto", "manual", "reactive"],
        force: bool = False,
    ) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
        context = self._context
        if context.compact_port is None:
            self._last_compaction_result = (messages, False)
            return

        progress_queue: asyncio.Queue[CompactProgressEvent] = asyncio.Queue()

        async def _progress(event: CompactProgressEvent) -> None:
            await progress_queue.put(event)

        task = asyncio.create_task(
            context.compact_port.compact_if_needed(
                messages,
                state=self._compact_state,
                api_client=context.api_client,
                model=context.model,
                system_prompt=context.system_prompt,
                progress_callback=_progress,
                force=force,
                trigger=trigger,
                hook_executor=context.hook_executor,
                carryover_metadata=context.tool_metadata,
                context_window_tokens=context.context_window_tokens,
                auto_compact_threshold_tokens=context.auto_compact_threshold_tokens,
            )
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(progress_queue.get(), timeout=0.05)
                    await _emit_event(context, "compact.progress", asdict(event))
                    yield event, None
                except TimeoutError:
                    if task.done():
                        break
                    continue
            while not progress_queue.empty():
                event = progress_queue.get_nowait()
                await _emit_event(context, "compact.progress", asdict(event))
                yield event, None
            self._last_compaction_result = await task
        finally:
            # If the consumer stops early (Ctrl-C, client disconnect), do not
            # leave compaction running in the background after the turn ends.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
