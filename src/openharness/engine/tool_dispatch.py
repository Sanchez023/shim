"""Tool-call dispatch: hooks, permissions, execution and artifact offload.

This is the default :class:`~openharness.runtime.seams.execution.ToolExecutor`.
It used to live inside ``query.py``, which forced the seam to import its own
consumer and left the default implementation in the legacy module.  Keeping it
here means the seam can depend on it directly, with no lazy import and no cycle.

The context is typed structurally so the dispatcher never has to import
``QueryContext``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from openharness.config.paths import get_data_dir
from openharness.domain.events import EventSequencer, EventSink
from openharness.domain.messages import ToolResultBlock
from openharness.engine import session_hygiene, verification_ledger
from openharness.engine.event_emit import emit_event
from openharness.engine.turn_state import (
    record_tool_carryover,
    remember_active_artifact,
)
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.permissions.command_safety import is_read_only_command, paths_in_command
from openharness.runtime.seams.governance.models import (
    SecurityDecision,
    SecurityRequest,
)
from openharness.tools.base import ToolExecutionContext, ToolRegistry
from openharness.utils.tool_outputs import tool_output_inline_chars, tool_output_preview_chars

log = logging.getLogger(__name__)

#: How long an unanswered approval prompt may hold a turn open.  A prompt that
#: nobody answers used to hang the turn until Ctrl-C; expiring is treated as a
#: refusal because that is the fail-closed answer (ADR-0012 D3).
APPROVAL_TIMEOUT_SECONDS = 600.0


async def _request_approval(
    gate: Any,
    tool_name: str,
    reason: str,
    context: ToolCallContext,
) -> bool:
    """Ask the user for approval, but never wait forever."""
    try:
        async with asyncio.timeout(APPROVAL_TIMEOUT_SECONDS):
            return bool(
                await gate.request(tool_name=tool_name, reason=reason, context=context)
            )
    except TimeoutError:
        log.warning(
            "approval prompt for %s expired after %.0fs; denying",
            tool_name,
            APPROVAL_TIMEOUT_SECONDS,
        )
        return False


class ToolCallContext(Protocol):
    """The slice of the query context a single tool call needs."""

    tool_registry: ToolRegistry
    permission_checker: PermissionChecker
    cwd: Path
    hook_executor: HookExecutor | None
    permission_prompt: Callable[[str, str], Awaitable[bool]] | None
    approval_gate: Any | None
    security_policy: Any | None
    tool_metadata: dict[str, object] | None
    event_sink: EventSink | None
    event_sequencer: EventSequencer | None
    turn_id: str
    current_step_id: str
    tool_executor: Any | None
    ask_user_prompt: Callable[..., Awaitable[str]] | None
    activate_tools: Callable[..., Any] | None


def tool_artifact_dir() -> Path:
    artifact_dir = get_data_dir() / "tool_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def safe_tool_artifact_name(tool_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name.strip())
    return (normalized or "tool")[:80]


def offload_tool_output_if_needed(
    *,
    tool_name: str,
    tool_use_id: str,
    output: str,
    inline_limit: int | None = None,
    preview_chars: int | None = None,
) -> tuple[str, Path | None]:
    limit = tool_output_inline_chars() if inline_limit is None else max(0, inline_limit)
    if len(output) <= limit:
        return output, None

    artifact_path = (
        tool_artifact_dir()
        / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_tool_artifact_name(tool_name)}-{uuid4().hex[:12]}.txt"
    )
    artifact_path.write_text(output, encoding="utf-8", errors="replace")
    preview_length = (
        tool_output_preview_chars() if preview_chars is None else max(0, preview_chars)
    )
    preview = output[:preview_length]
    omitted = max(0, len(output) - len(preview))
    inline = (
        "[Tool output truncated]\n"
        f"Tool: {tool_name}\n"
        f"Tool use id: {tool_use_id}\n"
        f"Original size: {len(output)} chars\n"
        f"Full output saved to: {artifact_path}\n"
        "Read it with the read_file tool (use offset/limit to page through it) "
        "instead of printing the whole file.\n"
        f"Inline preview: first {len(preview)} chars"
    )
    if omitted:
        inline += f" ({omitted} chars omitted)"
    if preview:
        inline += f"\n\nPreview:\n{preview}"
    return inline, artifact_path

async def dispatch_tool_call(
    context: ToolCallContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    """Dispatch through the tool-executor seam when one is installed."""
    if context.tool_executor is not None:
        return cast(
            ToolResultBlock,
            await context.tool_executor.execute(
                context, tool_name, tool_use_id, tool_input
            ),
        )
    return await execute_tool_call(context, tool_name, tool_use_id, tool_input)


async def execute_tool_call(
    context: ToolCallContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    if context.hook_executor is not None:
        pre_hooks = await context.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {"tool_name": tool_name, "tool_input": tool_input, "event": HookEvent.PRE_TOOL_USE.value},
        )
        if pre_hooks.blocked:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=pre_hooks.reason or f"pre_tool_use hook blocked {tool_name}",
                is_error=True,
            )

    log.debug("tool_call start: %s id=%s", tool_name, tool_use_id)

    tool = context.tool_registry.get(tool_name)
    if tool is None:
        log.warning("unknown tool: %s", tool_name)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Unknown tool: {tool_name}",
            is_error=True,
        )

    try:
        parsed_input = tool.input_model.model_validate(tool_input)
    except (ValueError, TypeError) as exc:
        log.warning("invalid input for %s: %s", tool_name, exc)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Invalid input for {tool_name}: {exc}",
            is_error=True,
        )

    # Normalize common tool inputs before permission checks so path rules apply
    # consistently across built-in tools that use `file_path`, `path`, or
    # directory-scoped roots such as `glob`/`grep`.
    _file_path = resolve_permission_file_path(context.cwd, tool_input, parsed_input)
    _command = extract_permission_command(tool_input, parsed_input)
    # Bash has no file_path input, so recover path-looking tokens from the
    # command for the sensitive-path checks.
    _paths: tuple[str, ...] = ()
    if _command and not _file_path:
        _paths = paths_in_command(_command)
    is_read_only = tool.is_read_only(parsed_input)
    _verdict_getter = getattr(tool, "read_only_verdict", None)
    _read_only_verdict = _verdict_getter(parsed_input) if callable(_verdict_getter) else None
    _reason_getter = getattr(tool, "read_only_reason", None)
    _read_only_reason = _reason_getter(parsed_input) if callable(_reason_getter) else None
    log.debug("permission check: %s read_only=%s verdict=%s path=%s paths=%s cmd=%s",
              tool_name, is_read_only, _read_only_verdict, _file_path, _paths,
              _command and _command[:80])
    security_request = SecurityRequest(
        tool_name=tool_name,
        is_read_only=is_read_only,
        file_path=_file_path,
        command=_command,
        paths=_paths,
        read_only_verdict=_read_only_verdict,
        read_only_reason=_read_only_reason,
        tool_input=dict(tool_input),
    )
    if context.security_policy is not None:
        decision = await context.security_policy.evaluate(security_request)
    else:
        legacy = context.permission_checker.evaluate(
            tool_name,
            is_read_only=security_request.is_read_only,
            file_path=_file_path,
            command=_command,
            paths=_paths,
            workspace_root=str(context.cwd),
            read_only_verdict=_read_only_verdict,
        )
        decision = SecurityDecision(
            allowed=legacy.allowed,
            requires_confirmation=legacy.requires_confirmation,
            reason=legacy.reason,
            dimension="tool",
        )
    await emit_event(
        context,
        "permission.decided",
        {
            "tool_name": tool_name,
            "dimension": decision.dimension,
            "decision": (
                "allow"
                if decision.allowed
                else "ask"
                if decision.requires_confirmation
                else "deny"
            ),
            "reason": decision.reason,
            "detail": {
                "path": _file_path,
                "command": _command,
                "read_only_verdict": _read_only_verdict,
                "read_only_reason": _read_only_reason,
            },
        },
    )
    if not decision.allowed:
        if decision.requires_confirmation:
            log.debug("permission prompt for %s: %s", tool_name, decision.reason)
            if context.hook_executor is not None:
                await context.hook_executor.execute(
                    HookEvent.NOTIFICATION,
                    {
                        "event": HookEvent.NOTIFICATION.value,
                        "notification_type": "permission_prompt",
                        "tool_name": tool_name,
                        "reason": decision.reason,
                    },
                )
            if context.approval_gate is not None:
                confirmed = await _request_approval(
                    context.approval_gate, tool_name, decision.reason, context
                )
            elif context.permission_prompt is not None:
                confirmed = await context.permission_prompt(tool_name, decision.reason)
            else:
                confirmed = False
            if not confirmed:
                log.debug("permission denied by user for %s", tool_name)
                return ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=decision.reason or f"Permission denied for {tool_name}",
                    is_error=True,
                )
        else:
            log.debug("permission blocked for %s: %s", tool_name, decision.reason)
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=decision.reason or f"Permission denied for {tool_name}",
                is_error=True,
            )

    log.debug("executing %s ...", tool_name)
    t0 = time.monotonic()
    # One signal per running call so Ctrl+B can detach it (Ctrl+B sets every
    # live event; only tools that know how to detach react to it).
    background_event = asyncio.Event()
    active_backgrounds = getattr(context, "active_tool_backgrounds", None)
    if active_backgrounds is not None:
        active_backgrounds.add(background_event)
    try:
        result = await tool.execute(
            parsed_input,
            ToolExecutionContext(
                cwd=context.cwd,
                metadata={
                    "tool_registry": context.tool_registry,
                    "ask_user_prompt": context.ask_user_prompt,
                    "activate_tools": context.activate_tools,
                    **(context.tool_metadata or {}),
                },
                hook_executor=context.hook_executor,
                agent_run_port=cast(
                    Any,
                    (
                        context.tool_metadata.get("agent_run_port")
                        if context.tool_metadata is not None
                        else None
                    ),
                ),
                background_event=background_event,
            ),
        )
    finally:
        if active_backgrounds is not None:
            active_backgrounds.discard(background_event)
    elapsed = time.monotonic() - t0
    log.debug("executed %s in %.2fs err=%s output_len=%d",
              tool_name, elapsed, result.is_error, len(result.output or ""))
    inline_output, artifact_path = offload_tool_output_if_needed(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        output=result.output,
    )
    if artifact_path is not None:
        remember_active_artifact(context.tool_metadata, str(artifact_path))
    tool_result = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=inline_output,
        is_error=result.is_error,
        result_metadata=dict(result.metadata or {}),
        images=list(result.images or []),
    )
    record_tool_carryover(
        context,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_result.content,
        tool_result_metadata=result.metadata,
        is_error=tool_result.is_error,
        resolved_file_path=_file_path,
    )
    # Ledger first, annotation second: the suffix has to describe the state
    # *after* this call, otherwise a green run still reports itself stale.
    verification_ledger.record_tool_call(
        context.tool_metadata,
        tool_name=tool_name,
        tool_input=tool_input,
        is_error=tool_result.is_error,
        resolved_file_path=_file_path,
        result_metadata=result.metadata,
        # Injected, not imported inside the ledger: the engine must not depend
        # on the permission layer (ADR-0010), and the classifier is already
        # loaded here.
        read_only=is_read_only_command,
    )
    # One git round-trip per session, not per call: the baseline is what makes
    # "this session left a worktree behind" answerable at turn end.
    await session_hygiene.ensure_baseline(
        context.tool_metadata,
        context.cwd,
        enabled=verification_ledger.hygiene_enabled(context.tool_metadata),
    )
    tool_result = _with_verification_status(
        tool_result,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_metadata=context.tool_metadata,
        offloaded=artifact_path is not None,
    )
    if context.hook_executor is not None:
        await context.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_result.content,
                "tool_is_error": tool_result.is_error,
                "event": HookEvent.POST_TOOL_USE.value,
            },
        )
    return tool_result


def _with_verification_status(
    tool_result: ToolResultBlock,
    *,
    tool_name: str,
    tool_input: dict[str, object],
    tool_metadata: dict[str, object] | None,
    offloaded: bool,
) -> ToolResultBlock:
    """Append the ledger's one-line status to calls that change what is verified.

    Only edit tools and classified verification commands are annotated: those
    are the two moments where "is this still verified?" is the question the
    model needs answered, and a tool result is the only channel it reliably
    reads back.  Annotating every call would be noise.

    Offloaded output is skipped: the model only sees a pointer there, so a
    status line hanging off it would describe an artifact, not the run.
    """

    if offloaded:
        return tool_result
    if tool_name not in verification_ledger.EDIT_TOOL_NAMES:
        if tool_name != "bash":
            return tool_result
        command = tool_input.get("command")
        if not isinstance(command, str):
            return tool_result
        if verification_ledger.classify_command(command) is None:
            return tool_result
    line = verification_ledger.tool_status_line(tool_metadata)
    if not line:
        return tool_result
    content = f"{tool_result.content}\n\n{line}" if tool_result.content else line
    return tool_result.model_copy(update={"content": content})


def resolve_permission_file_path(
    cwd: Path,
    raw_input: dict[str, object],
    parsed_input: object,
) -> str | None:
    for key in ("file_path", "path", "root"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = cwd / path
            return str(path.resolve())

    for attr in ("file_path", "path", "root"):
        value = getattr(parsed_input, attr, None)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = cwd / path
            return str(path.resolve())

    return None


def extract_permission_command(
    raw_input: dict[str, object],
    parsed_input: object,
) -> str | None:
    value = raw_input.get("command")
    if isinstance(value, str) and value.strip():
        return value

    value = getattr(parsed_input, "command", None)
    if isinstance(value, str) and value.strip():
        return value

    return None
