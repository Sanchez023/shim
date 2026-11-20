"""Shared runtime assembly for headless and interactive UIs."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from openharness.api.provider import (
    auth_status,
    detect_provider,
)
from openharness.assembly import (
    RuntimeBundle,
    _last_user_text,
    _resolve_api_client_from_settings,
    build_runtime,
    close_runtime,
    start_runtime,
)
from openharness.bridge import get_bridge_manager
from openharness.commands import (
    CommandContext,
    CommandResult,
    lookup_skill_slash_command,
)
from openharness.commands.registry import CommandRegistry
from openharness.domain.messages import (
    ConversationMessage,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.domain.stream_events import StreamEvent
from openharness.engine.query import MaxTurnsExceeded
from openharness.hooks import load_hook_registry
from openharness.keybindings import load_keybindings
from openharness.permissions import PermissionChecker
from openharness.prompts import (
    build_stable_system_prompt_result,
)
from openharness.ui.protocol import TranscriptItem

logger = logging.getLogger(__name__)

# Compatibility re-exports: the assembly implementation moved to
# ``openharness.assembly`` / ``openharness.services.assembly`` (ADR-0010 D3).
__all__ = [
    "RuntimeBundle",
    "_last_user_text",
    "_resolve_api_client_from_settings",
    "build_runtime",
    "close_runtime",
    "start_runtime",
]

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[..., Awaitable[str]]
EditApprovalPrompt = Callable[[str, str, int, int], Awaitable[str]]
SystemPrinter = Callable[[str], Awaitable[None]]
StreamRenderer = Callable[[StreamEvent], Awaitable[None]]
ClearHandler = Callable[[], Awaitable[None]]
ItemRenderer = Callable[[TranscriptItem], Awaitable[None]]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _format_pending_tool_results(messages: list[ConversationMessage]) -> str | None:
    """Render a compact summary when we stop after tool execution but before the follow-up model turn."""
    if not messages:
        return None

    last = messages[-1]
    if last.role != "user":
        return None
    tool_results = [block for block in last.content if isinstance(block, ToolResultBlock)]
    if not tool_results:
        return None

    tool_uses_by_id: dict[str, ToolUseBlock] = {}
    assistant_text = ""
    for msg in reversed(messages[:-1]):
        if msg.role != "assistant":
            continue
        if not msg.tool_uses:
            continue
        assistant_text = msg.text.strip()
        for tu in msg.tool_uses:
            tool_uses_by_id[tu.id] = tu
        break

    lines: list[str] = [
        "Pending continuation: tool results were produced, but the model did not get a chance to respond yet."
    ]
    if assistant_text:
        lines.append(f"Last assistant message: {_truncate(assistant_text, 400)}")

    max_results = 3
    for tr in tool_results[:max_results]:
        matched = tool_uses_by_id.get(tr.tool_use_id)
        if matched is not None:
            raw_input = json.dumps(matched.input, ensure_ascii=True, sort_keys=True)
            lines.append(
                f"- {matched.name} {_truncate(raw_input, 200)}"
                f" -> {_truncate(tr.content.strip(), 400)}"
            )
        else:
            lines.append(
                f"- tool_result[{tr.tool_use_id}] -> {_truncate(tr.content.strip(), 400)}"
            )

    if len(tool_results) > max_results:
        lines.append(f"(+{len(tool_results) - max_results} more tool results)")

    lines.append("To continue from these results, run: /continue [COUNT].")
    return "\n".join(lines)


def sync_app_state(bundle: RuntimeBundle) -> None:
    """Refresh UI state from current settings and dynamic keybindings."""
    settings = bundle.current_settings()
    if bundle.enforce_max_turns:
        bundle.engine.set_max_turns(settings.max_turns)
    provider = detect_provider(settings)
    bundle.app_state.set(
        model=settings.model,
        permission_mode=settings.permission.mode.value,
        theme=settings.theme,
        cwd=bundle.cwd,
        provider=provider.name,
        auth_status=auth_status(settings),
        base_url=settings.base_url or "",
        vim_enabled=settings.vim_mode,
        voice_enabled=settings.voice_mode,
        voice_available=provider.voice_supported,
        voice_reason=provider.voice_reason,
        fast_mode=settings.fast_mode,
        effort=settings.effort,
        passes=settings.passes,
        personality=settings.personality,
        tool_profile=settings.tool_profile,
        mcp_connected=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "connected"),
        mcp_failed=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "failed"),
        bridge_sessions=len(get_bridge_manager().list_sessions()),
        output_style=settings.output_style,
        keybindings=load_keybindings(),
    )


def refresh_runtime_client(bundle: RuntimeBundle) -> None:
    """Refresh the active runtime client after provider/auth/profile changes."""
    settings = bundle.current_settings()
    if not bundle.external_api_client:
        bundle.api_client = _resolve_api_client_from_settings(settings)
        bundle.engine.set_api_client(bundle.api_client)
        bundle.hook_executor.update_context(
            api_client=bundle.api_client,
            default_model=settings.model,
        )
    bundle.engine.set_model(settings.model)
    bundle.engine.set_effort(settings.effort)
    bundle.engine.set_permission_checker(PermissionChecker(settings.permission))
    bundle.engine.set_system_prompt_result(
        build_stable_system_prompt_result(
            settings,
            cwd=bundle.cwd,
            extra_skill_dirs=bundle.extra_skill_dirs,
            extra_plugin_roots=bundle.extra_plugin_roots,
            available_tools={tool.name for tool in bundle.tool_registry.list_tools()},
        )
    )
    bundle.engine.refresh_turn_context()
    sync_app_state(bundle)


async def handle_line(
    bundle: RuntimeBundle,
    line: str,
    *,
    print_system: SystemPrinter,
    render_event: StreamRenderer,
    clear_output: ClearHandler,
    user_message: ConversationMessage | None = None,
    print_user: SystemPrinter | None = None,
    render_item: ItemRenderer | None = None,
) -> bool:
    """Handle one submitted line for either headless or TUI rendering."""
    if not bundle.external_api_client:
        bundle.hook_executor.update_registry(
            load_hook_registry(bundle.current_settings(), bundle.current_plugins())
        )

    command_context = CommandContext(
        engine=bundle.engine,
        hooks_summary=bundle.hook_summary(),
        mcp_summary=bundle.mcp_summary(),
        plugin_summary=bundle.plugin_summary(),
        cwd=bundle.cwd,
        tool_registry=bundle.tool_registry,
        app_state=bundle.app_state,
        session_backend=bundle.session_backend,
        session_id=bundle.session_id,
        extra_skill_dirs=bundle.extra_skill_dirs,
        extra_plugin_roots=bundle.extra_plugin_roots,
        memory_backend=bundle.memory_backend,
        include_project_memory=bundle.include_project_memory,
        settings_overrides=bundle.settings_overrides,
        print_system=print_system,
    )
    # ``RuntimeBundle.commands`` is typed as ``object`` on purpose (services do
    # not import the command layer, ADR-0010); the presentation layer knows the
    # concrete registry type.
    registry = bundle.commands if isinstance(bundle.commands, CommandRegistry) else None
    parsed = None if user_message is not None else (
        (registry.lookup(line) if registry is not None else None)
        or lookup_skill_slash_command(line, command_context)
    )
    if parsed is not None:
        command, args = parsed
        result = await command.handler(
            args,
            command_context,
        )
        if result.refresh_runtime:
            refresh_runtime_client(bundle)
        await _render_command_result(
            result,
            print_system,
            clear_output,
            render_event,
            print_user=print_user,
            render_item=render_item,
        )
        if result.submit_prompt is not None:
            original_model = bundle.engine.model
            if result.submit_model:
                bundle.engine.set_model(result.submit_model)
            settings = bundle.current_settings()
            submit_prompt = result.submit_prompt
            bundle.engine.set_system_prompt_result(
                build_stable_system_prompt_result(
                    settings,
                    cwd=bundle.cwd,
                    extra_skill_dirs=bundle.extra_skill_dirs,
                    extra_plugin_roots=bundle.extra_plugin_roots,
                    available_tools={
                        tool.name for tool in bundle.tool_registry.list_tools()
                    },
                )
            )
            try:
                async for event in bundle.backend().submit(submit_prompt):
                    await render_event(event)
            except MaxTurnsExceeded as exc:
                await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
                pending = _format_pending_tool_results(bundle.engine.messages)
                if pending:
                    await print_system(pending)
            finally:
                if result.submit_model:
                    bundle.engine.set_model(original_model)
            bundle.session_backend.save_snapshot(
                cwd=bundle.cwd,
                model=bundle.engine.model,
                system_prompt=bundle.engine.system_prompt,
                messages=bundle.engine.messages,
                usage=bundle.engine.total_usage,
                session_id=bundle.session_id,
                tool_metadata=bundle.engine.tool_metadata,
            )
        if result.continue_pending:
            settings = bundle.current_settings()
            if bundle.enforce_max_turns:
                bundle.engine.set_max_turns(settings.max_turns)
            bundle.engine.set_system_prompt_result(
                build_stable_system_prompt_result(
                    settings,
                    cwd=bundle.cwd,
                    extra_skill_dirs=bundle.extra_skill_dirs,
                    extra_plugin_roots=bundle.extra_plugin_roots,
                    available_tools={
                        tool.name for tool in bundle.tool_registry.list_tools()
                    },
                )
            )
            turns = result.continue_turns if result.continue_turns is not None else bundle.engine.max_turns
            try:
                async for event in bundle.backend().resume(max_turns=turns):
                    await render_event(event)
            except MaxTurnsExceeded as exc:
                await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
                pending = _format_pending_tool_results(bundle.engine.messages)
                if pending:
                    await print_system(pending)
            bundle.session_backend.save_snapshot(
                cwd=bundle.cwd,
                model=settings.model,
                system_prompt=bundle.engine.system_prompt,
                messages=bundle.engine.messages,
                usage=bundle.engine.total_usage,
                session_id=bundle.session_id,
                tool_metadata=bundle.engine.tool_metadata,
            )
        sync_app_state(bundle)
        return not result.should_exit

    settings = bundle.current_settings()
    if bundle.enforce_max_turns:
        bundle.engine.set_max_turns(settings.max_turns)
    bundle.engine.set_system_prompt_result(
        build_stable_system_prompt_result(
            settings,
            cwd=bundle.cwd,
            extra_skill_dirs=bundle.extra_skill_dirs,
            extra_plugin_roots=bundle.extra_plugin_roots,
            available_tools={tool.name for tool in bundle.tool_registry.list_tools()},
        )
    )
    try:
        async for event in bundle.backend().submit(user_message or line):
            await render_event(event)
    except MaxTurnsExceeded as exc:
        await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
        pending = _format_pending_tool_results(bundle.engine.messages)
        if pending:
            await print_system(pending)
        bundle.session_backend.save_snapshot(
            cwd=bundle.cwd,
            model=settings.model,
            system_prompt=bundle.engine.system_prompt,
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
            tool_metadata=bundle.engine.tool_metadata,
        )
        sync_app_state(bundle)
        return True
    bundle.session_backend.save_snapshot(
        cwd=bundle.cwd,
        model=settings.model,
        system_prompt=bundle.engine.system_prompt,
        messages=bundle.engine.messages,
        usage=bundle.engine.total_usage,
        session_id=bundle.session_id,
        tool_metadata=bundle.engine.tool_metadata,
    )
    sync_app_state(bundle)
    return True


async def _render_command_result(
    result: CommandResult,
    print_system: SystemPrinter,
    clear_output: ClearHandler,
    render_event: StreamRenderer | None = None,
    print_user: SystemPrinter | None = None,
    render_item: ItemRenderer | None = None,
) -> None:
    if result.clear_screen:
        await clear_output()
    if result.replay_messages and render_event is not None:
        # Replay restored conversation messages as transcript events
        from openharness.domain.messages import strip_private_blocks
        from openharness.domain.stream_events import AssistantTextDelta, AssistantTurnComplete
        from openharness.domain.usage import UsageSnapshot

        await clear_output()
        await print_system("Session restored:")
        if render_item is not None:
            # TUI: replay the same rows a live turn produces (tool calls are
            # collapsible, harness context is dropped). A flat text replay made
            # a resumed conversation look nothing like the session it restores.
            from openharness.ui.transcript_replay import transcript_items_for_messages

            for item in transcript_items_for_messages(result.replay_messages):
                await render_item(item)
        else:
            emit_user = print_user or print_system
            for msg in result.replay_messages:
                # Replay only what the user was allowed to see: harness-injected
                # blocks (the per-turn context message) are session_private and
                # must not show up as a giant user bubble after /resume.
                visible = strip_private_blocks(msg)
                if visible.role == "user" and visible.text.strip():
                    await emit_user(visible.text)
                elif visible.role == "assistant" and visible.text.strip():
                    await render_event(AssistantTextDelta(text=visible.text))
                    await render_event(
                        AssistantTurnComplete(message=visible, usage=UsageSnapshot())
                    )
    if result.message and not result.replay_messages:
        await print_system(result.message)
