"""Single-step execution: one model request plus its tool batch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiTextDeltaEvent,
    ApiThinkingDeltaEvent,
)
from openharness.api.finish_reason import FinishReason, is_truncated
from openharness.domain.messages import (
    ContentBlock,
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    visibility_for_message,
)
from openharness.domain.stream_events import (
    AssistantTextDelta,
    AssistantThinkingDelta,
    AssistantTurnComplete,
    ContextChangedEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.domain.usage import UsageSnapshot
from openharness.engine import session_hygiene, verification_ledger
from openharness.engine.cache_budget import shrink_tool_results_for_budget
from openharness.engine.cache_diagnostics import fingerprint_request
from openharness.engine.event_emit import emit_event as _emit_event
from openharness.engine.step_support import _preprocess_images_in_messages
from openharness.engine.tool_dispatch import dispatch_tool_call as _execute_tool_call
from openharness.hooks import HookEvent
from openharness.services.compact.tokens import record_prompt_measurement

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openharness.engine.query import QueryContext

log = logging.getLogger(__name__)


def _take_tool_images(
    tool_calls: list[ToolUseBlock],
    tool_results: list[ToolResultBlock],
) -> list[ContentBlock]:
    """Move images produced by tools into the tool-results user message.

    Providers disagree about images *inside* a tool result: Anthropic supports
    them, the OpenAI chat shape cannot express them.  Every adapter does accept
    an image block in a user message, so the images ride alongside the results
    as siblings.  The tool-result blocks are cleared afterwards so the payload
    is not carried twice.
    """
    blocks: list[ContentBlock] = []
    names = {call.id: call.name for call in tool_calls}
    for result in tool_results:
        if not result.images:
            continue
        tool_name = names.get(result.tool_use_id, "tool")
        blocks.append(TextBlock(text=f"[image from {tool_name}]"))
        blocks.extend(
            image.model_copy(update={"origin_tool_use_id": result.tool_use_id})
            for image in result.images
        )
        result.images = []
    return blocks


@dataclass
class StepOutcome:
    """Facts produced by one step."""

    final_message: ConversationMessage | None = None
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    needs_followup: bool = False
    status: str = "completed"
    step_end_emitted: bool = False


class StepRunner:
    """Run one model step and, when requested, its tool batch."""

    def __init__(self, context: QueryContext) -> None:
        self._context: QueryContext = context
        self.outcome = StepOutcome()

    async def run(
        self,
        messages: list[ConversationMessage],
        *,
        step_id: str,
        max_tokens: int,
        turn_count: int,
    ) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
        context = self._context
        outcome = StepOutcome()
        self.outcome = outcome
        del turn_count  # step.start is emitted by the turn executor before compaction

        async for event in _preprocess_images_in_messages(messages, context):
            if isinstance(event, StatusEvent):
                await _emit_event(
                    context,
                    "status",
                    {"message": event.message},
                    step_id=step_id,
                )
            yield event, None

        final_message: ConversationMessage | None = None
        usage = UsageSnapshot()
        finish_reason = FinishReason.UNKNOWN
        tool_schemas = context.tool_registry.to_api_schema(
            context.tool_schema_names,
            append_order=getattr(context, "tool_append_order", ()),
        )
        request_fingerprint = None
        cache_diagnostics = getattr(context, "cache_diagnostics", None)
        if cache_diagnostics is not None:
            request_fingerprint = fingerprint_request(
                model=context.model,
                system_prompt=context.system_prompt,
                tools=tool_schemas,
                # Fingerprint the persisted history, not the request-only turn
                # context. The context is deliberately new each turn; treating
                # it as history would report a false history_rewrite.
                messages=messages,
                section_hashes=getattr(context, "system_prompt_section_hashes", ()),
                system_prompt_cacheable_prefix=getattr(
                    context, "system_prompt_cacheable_prefix", None
                ),
            )

        # The provider bills this exact list; remember its size so the next
        # compaction check can anchor on real tokens instead of an estimate.
        request_message_count = len(messages)

        # Tell the cache keepalive a real request is in flight so it never
        # refreshes the same prefix concurrently, and so its idle timer is
        # anchored on model traffic rather than on tool runtime.
        note_start = getattr(context, "note_model_request_start", None)
        note_end = getattr(context, "note_model_request_end", None)
        if callable(note_start):
            note_start()
        try:
            async for api_event in context.api_client.stream_message(
                ApiMessageRequest(
                    model=context.model,
                    messages=messages,
                    system_prompt=context.system_prompt,
                    system_prompt_cacheable_prefix=context.system_prompt_cacheable_prefix,
                    max_tokens=max_tokens,
                    tools=tool_schemas,
                    effort=context.effort,
                )
            ):
                if isinstance(api_event, ApiTextDeltaEvent):
                    await _emit_event(
                        context,
                        "assistant.text.delta",
                        {"text": api_event.text},
                        step_id=step_id,
                    )
                    yield AssistantTextDelta(text=api_event.text), None
                    continue
                if isinstance(api_event, ApiThinkingDeltaEvent):
                    await _emit_event(
                        context,
                        "assistant.thinking.delta",
                        {"text": api_event.text},
                        step_id=step_id,
                    )
                    yield AssistantThinkingDelta(text=api_event.text), None
                    continue
                if isinstance(api_event, ApiRetryEvent):
                    status_message = (
                        f"Request failed; retrying in {api_event.delay_seconds:.1f}s "
                        f"(attempt {api_event.attempt + 1} of {api_event.max_attempts}): {api_event.message}"
                    )
                    await _emit_event(
                        context,
                        "status",
                        {"message": status_message},
                        step_id=step_id,
                    )
                    yield StatusEvent(message=status_message), None
                    continue
                if isinstance(api_event, ApiMessageCompleteEvent):
                    final_message = api_event.message
                    usage = api_event.usage
                    finish_reason = api_event.finish_reason
        finally:
            if callable(note_end):
                note_end()

        if final_message is None:
            raise RuntimeError("Model stream finished without a final message")

        cache_info: dict[str, Any] = {}
        if cache_diagnostics is not None and request_fingerprint is not None:
            cache_info = cache_diagnostics.record(request_fingerprint, usage)
            if cache_info.get("alert") and cache_info.get("alert_message"):
                await _emit_event(
                    context,
                    "status",
                    {"message": cache_info["alert_message"]},
                    step_id=step_id,
                )
                yield StatusEvent(message=cache_info["alert_message"]), None

        coordinator_context_message: ConversationMessage | None = None
        if (
            context.system_prompt.startswith("You are a **coordinator**.")
            and messages
            and messages[-1].role == "user"
            and messages[-1].text.startswith("# Coordinator User Context")
        ):
            coordinator_context_message = messages.pop()

        if final_message.role == "assistant" and final_message.is_effectively_empty():
            log.warning("dropping empty assistant message from provider response")
            error_message = (
                "Model returned an empty assistant message. "
                "The turn was ignored to keep the session healthy."
            )
            await _emit_event(
                context,
                "error",
                {"message": error_message, "recoverable": True},
                step_id=step_id,
            )
            await _emit_event(
                context,
                "step.end",
                {"status": "empty_assistant"},
                step_id=step_id,
            )
            outcome.final_message = final_message
            outcome.usage = usage
            outcome.status = "empty_assistant"
            outcome.step_end_emitted = True
            yield ErrorEvent(message=error_message), usage
            return

        messages.append(final_message)
        record_prompt_measurement(
            context.tool_metadata,
            message_count=request_message_count,
            prompt_tokens=usage.prompt_tokens,
        )
        await _emit_event(
            context,
            "assistant.message",
            {
                "message": final_message.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
                "cache": cache_info,
                "finish_reason": finish_reason.value,
            },
            step_id=step_id,
            visibility=visibility_for_message(final_message),  # type: ignore[arg-type]
        )
        truncated = is_truncated(finish_reason)
        if truncated:
            # The provider cut the answer off at max_tokens.  Treating that as a
            # normal completion silently persists a half-finished file, patch or
            # plan, so say so before the turn ends.
            truncation_message = (
                "Response truncated at max_tokens "
                f"({context.max_tokens}). The output above is incomplete; "
                "raise max_tokens or ask the model to continue."
            )
            await _emit_event(
                context,
                "status",
                {"message": truncation_message, "finish_reason": finish_reason.value},
                step_id=step_id,
            )
            yield StatusEvent(message=truncation_message), None
        elif finish_reason is FinishReason.UNKNOWN:
            # The adapter never saw a completion signal (``message_stop`` /
            # ``[DONE]`` / a non-null ``finish_reason``).  Some providers simply
            # do not send one, so this is a warning rather than a failure -- but
            # a reply that cannot be proven complete must not be presented as
            # one (ADR-0012 D2).
            incomplete_message = (
                "Stream ended without a completion signal; this reply may be "
                "incomplete. Re-run the request if it ends mid-sentence."
            )
            await _emit_event(
                context,
                "status",
                {"message": incomplete_message, "finish_reason": finish_reason.value},
                step_id=step_id,
            )
            yield StatusEvent(message=incomplete_message), None

        # Emitted after the caveats so that "turn complete" is never the last
        # word when the answer could not be proven complete.
        yield AssistantTurnComplete(message=final_message, usage=usage), usage

        if coordinator_context_message is not None:
            messages.append(coordinator_context_message)

        if not final_message.tool_uses:
            # STOP hooks used to be fired and their verdict dropped, so a hook
            # configured with ``block_on_failure`` could never block anything.
            # The verdict now feeds the same one-shot reminder as the
            # verification gate, which is what bounds it: one correction per
            # unverified streak, so a misconfigured hook cannot loop.
            stop_block_reason = await _run_stop_hooks(context)
            hygiene_notes = await session_hygiene.collect(
                context.tool_metadata,
                context.cwd,
                enabled=verification_ledger.hygiene_enabled(context.tool_metadata),
            )
            nudge_text = _completion_nudge(
                context,
                stop_block_reason=stop_block_reason,
                hygiene_notes=hygiene_notes,
            )
            if nudge_text is not None:
                nudge_message = ConversationMessage.from_user_text(nudge_text)
                messages.append(nudge_message)
                notice = nudge_text.splitlines()[0]
                await _emit_event(
                    context,
                    "status",
                    {"message": notice},
                    step_id=step_id,
                )
                yield StatusEvent(message=notice), None
                await _emit_event(
                    context,
                    "user.message",
                    {"message": nudge_message.model_dump(mode="json"), "synthetic": True},
                    step_id=step_id,
                )
                await _emit_event(
                    context,
                    "step.end",
                    {"status": "completion_nudge"},
                    step_id=step_id,
                )
                outcome.final_message = final_message
                outcome.usage = usage
                outcome.needs_followup = True
                outcome.status = "completion_nudge"
                outcome.step_end_emitted = True
                return

            await _emit_event(
                context,
                "step.end",
                {"status": "completed"},
                step_id=step_id,
            )
            outcome.final_message = final_message
            outcome.usage = usage
            outcome.status = "completed"
            outcome.step_end_emitted = True
            return

        tool_calls = final_message.tool_uses
        tool_results: list[ToolResultBlock]
        if truncated:
            # A tool call produced by a cut-off response can write half a file:
            # the arguments are valid JSON often enough to be parsed and run.
            # Record every call as "not executed" so the tool_use/tool_result
            # pairing stays valid and the model can decide to retry (ADR-0012
            # D1).  Nothing in this branch touches the workspace.
            tool_results = [
                ToolResultBlock(
                    tool_use_id=tc.id,
                    content=(
                        "Not executed: the response was truncated at max_tokens "
                        "before this call completed. Nothing was run. Raise "
                        "max_tokens, or reissue the call in smaller steps."
                    ),
                    is_error=True,
                )
                for tc in tool_calls
            ]
            for tc, tool_result in zip(tool_calls, tool_results):
                await _emit_event(
                    context,
                    "tool.result",
                    {
                        "tool_name": tc.name,
                        "tool_use_id": tc.id,
                        "output": tool_result.content,
                        "is_error": True,
                        "metadata": tool_result.result_metadata,
                    },
                    step_id=step_id,
                )
                yield ToolExecutionCompleted(
                    tool_name=tc.name,
                    output=tool_result.content,
                    is_error=True,
                    metadata=tool_result.result_metadata,
                    tool_use_id=tc.id,
                ), None
        elif len(tool_calls) == 1:
            tc = tool_calls[0]
            await _emit_event(
                context,
                "tool.call",
                {"tool_name": tc.name, "tool_input": tc.input, "tool_use_id": tc.id},
                step_id=step_id,
            )
            yield ToolExecutionStarted(
                tool_name=tc.name,
                tool_input=tc.input,
                tool_use_id=tc.id,
            ), None
            try:
                result = await _execute_tool_call(context, tc.name, tc.id, tc.input)
            except Exception as exc:
                log.exception("tool execution raised: name=%s id=%s", tc.name, tc.id)
                result = ToolResultBlock(
                    tool_use_id=tc.id,
                    content=f"Tool {tc.name} failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            await _emit_event(
                context,
                "tool.result",
                {
                    "tool_name": tc.name,
                    "tool_use_id": tc.id,
                    "output": result.content,
                    "is_error": result.is_error,
                    "metadata": result.result_metadata,
                },
                step_id=step_id,
            )
            yield ToolExecutionCompleted(
                tool_name=tc.name,
                output=result.content,
                is_error=result.is_error,
                metadata=result.result_metadata,
                tool_use_id=tc.id,
            ), None
            tool_results = [result]
        else:
            for tc in tool_calls:
                await _emit_event(
                    context,
                    "tool.call",
                    {"tool_name": tc.name, "tool_input": tc.input, "tool_use_id": tc.id},
                    step_id=step_id,
                )
                yield ToolExecutionStarted(
                    tool_name=tc.name,
                    tool_input=tc.input,
                    tool_use_id=tc.id,
                ), None

            async def _run(tc: ToolUseBlock) -> ToolResultBlock:
                return await _execute_tool_call(context, tc.name, tc.id, tc.input)

            def _convert(tc: ToolUseBlock, raw_result: object) -> ToolResultBlock:
                if isinstance(raw_result, asyncio.CancelledError):
                    # A cancelled tool means the turn is being torn down, not that
                    # the tool failed.  Converting it into an is_error result made
                    # cancellation look like a normal failure and let the rest of
                    # the batch keep running (ADR-0012 D3).
                    raise raw_result
                if isinstance(raw_result, BaseException):
                    log.exception(
                        "tool execution raised: name=%s id=%s",
                        tc.name,
                        tc.id,
                        exc_info=raw_result,
                    )
                    return ToolResultBlock(
                        tool_use_id=tc.id,
                        content=(
                            f"Tool {tc.name} failed: "
                            f"{type(raw_result).__name__}: {raw_result}"
                        ),
                        is_error=True,
                    )
                return cast(ToolResultBlock, raw_result)

            # Permission-mode switches must apply before the other calls in
            # the same assistant message are evaluated; running the batch
            # concurrently let a write race ahead of enter_plan_mode.
            if any(tc.name in {"enter_plan_mode", "exit_plan_mode"} for tc in tool_calls):
                tool_results = []
                for tc in tool_calls:
                    try:
                        raw: object = await _run(tc)
                    except Exception as exc:  # noqa: BLE001 - converted below
                        raw = exc
                    tool_result = _convert(tc, raw)
                    tool_results.append(tool_result)
                    await _emit_event(
                        context,
                        "tool.result",
                        {
                            "tool_name": tc.name,
                            "tool_use_id": tc.id,
                            "output": tool_result.content,
                            "is_error": tool_result.is_error,
                            "metadata": tool_result.result_metadata,
                        },
                        step_id=step_id,
                    )
                    yield ToolExecutionCompleted(
                        tool_name=tc.name,
                        output=tool_result.content,
                        is_error=tool_result.is_error,
                        metadata=tool_result.result_metadata,
                        tool_use_id=tc.id,
                    ), None
            else:
                # Report each tool as soon as it finishes instead of waiting for
                # the slowest one.  `gather` used to hold every completion until
                # the whole batch was done, so a five-second bash call beside a
                # five-minute subagent only appeared (and was timed) after the
                # subagent finished.
                scheduled = {asyncio.create_task(_run(tc)): tc for tc in tool_calls}
                order = {tc.id: index for index, tc in enumerate(tool_calls)}
                pending = set(scheduled)
                results: dict[str, ToolResultBlock] = {}
                try:
                    while pending:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in sorted(
                            done, key=lambda item: order[scheduled[item].id]
                        ):
                            tc = scheduled[task]
                            try:
                                raw = task.result()
                            except asyncio.CancelledError:
                                for outstanding in pending:
                                    outstanding.cancel()
                                raise
                            except BaseException as exc:  # noqa: BLE001 - converted
                                raw = exc
                            tool_result = _convert(tc, raw)
                            results[tc.id] = tool_result
                            await _emit_event(
                                context,
                                "tool.result",
                                {
                                    "tool_name": tc.name,
                                    "tool_use_id": tc.id,
                                    "output": tool_result.content,
                                    "is_error": tool_result.is_error,
                                    "metadata": tool_result.result_metadata,
                                },
                                step_id=step_id,
                            )
                            yield ToolExecutionCompleted(
                                tool_name=tc.name,
                                output=tool_result.content,
                                is_error=tool_result.is_error,
                                metadata=tool_result.result_metadata,
                                tool_use_id=tc.id,
                            ), None
                finally:
                    # A closed/cancelled turn must not leave tools running.
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                tool_results = [results[tc.id] for tc in tool_calls]

        # Keep the next request's new tool tokens inside the cache budget.
        # The full output is preserved as an artifact; only the inline preview
        # is shortened, and both the model-visible message and the persisted
        # history see the same shortened content.
        shrink_tool_results_for_budget(
            tool_results,
            tool_names={tc.id: tc.name for tc in tool_calls},
            previous_prompt_tokens=usage.prompt_tokens,
        )

        tool_images = _take_tool_images(tool_calls, tool_results)
        tool_results_message = ConversationMessage(
            role="user",
            content=cast(list[ContentBlock], [*tool_results, *tool_images]),
        )
        messages.append(tool_results_message)
        # The next request carries these results, so refresh context meters now
        # instead of after the model's next (possibly slow) response.
        yield ContextChangedEvent(), None
        await _emit_event(
            context,
            "user.message",
            {
                # The projection deliberately drops image payloads: the TUI
                # renders no images and base64 through the stdout pipe is pure
                # overhead.  The persisted history keeps them.
                "message": ConversationMessage(
                    role="user",
                    content=cast(list[ContentBlock], tool_results),
                ).model_dump(mode="json"),
                "synthetic": True,
            },
            step_id=step_id,
        )
        await _emit_event(
            context,
            "step.end",
            {"status": "tool_results"},
            step_id=step_id,
        )
        outcome.final_message = final_message
        outcome.usage = usage
        outcome.needs_followup = True
        outcome.status = "tool_results"
        outcome.step_end_emitted = True


async def _run_stop_hooks(context: QueryContext) -> str:
    """Fire the STOP hooks and return a blocking reason, or ``""``.

    Only blocking results are surfaced; a hook that merely failed is already
    visible in its own output and should not nag the model.
    """

    if context.hook_executor is None:
        return ""
    result = await context.hook_executor.execute(
        HookEvent.STOP,
        {"event": HookEvent.STOP.value, "stop_reason": "tool_uses_empty"},
    )
    if not result.blocked:
        return ""
    return (result.reason or "the hook reported a failure").strip()[:400]


def _completion_nudge(
    context: QueryContext,
    *,
    stop_block_reason: str,
    hygiene_notes: list[str],
) -> str | None:
    """Return the reminder to inject before this turn is allowed to end.

    Three independent reasons can hold a turn open: a blocked STOP hook, an
    edit the ledger cannot prove was verified, and a workspace leftover.  They
    share one budget (``verification.nudge_limit``) because the failure mode
    this guards against is a loop, not a shortage of reminders -- and they are
    merged into one message so the model answers them in a single pass.
    """

    metadata = context.tool_metadata
    parts: list[str] = []
    if stop_block_reason:
        parts.append(verification_ledger.stop_block_nudge(stop_block_reason))
    else:
        gate = verification_ledger.status(
            metadata, nudge_limit=verification_ledger.nudge_limit_from(metadata)
        )
        if gate.needs_verification:
            parts.append(verification_ledger.completion_nudge(gate))
    if hygiene_notes:
        parts.append(session_hygiene.render(hygiene_notes))
    if not parts:
        return None
    if not verification_ledger.nudge_budget_left(metadata):
        return None
    verification_ledger.note_nudge(metadata)
    return "\n\n".join(parts)
