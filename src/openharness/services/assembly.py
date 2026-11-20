"""Runtime assembly shared by the CLI, TUI, ohmo, and autopilot.

This is the layer-4 composition root (ADR-0010 D2/D3). The command registry is
supplied by the presentation layer so services never import commands.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openharness.api.client import AnthropicApiClient, SupportsStreamingMessages
from openharness.api.codex_client import CodexApiClient
from openharness.api.copilot_client import CopilotClient
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.provider import (
    auth_status,
    detect_provider,
    supports_implicit_prompt_cache,
)
from openharness.bridge import get_bridge_manager
from openharness.config import Settings, get_config_file_path, load_settings, save_settings
from openharness.config.provider_types import ResolvedAuth
from openharness.domain.messages import (
    ConversationMessage,
    sanitize_conversation_messages,
)
from openharness.domain.stream_events import StreamEvent
from openharness.engine import QueryEngine
from openharness.engine.cache_warmup import get_default_cache_warmup
from openharness.engine.session_manager import SessionManager
from openharness.hooks import (
    HookEvent,
    HookExecutionContext,
    HookExecutor,
    HookRegistry,
    load_hook_registry,
)
from openharness.hooks.hot_reload import HookReloader
from openharness.keybindings import load_keybindings
from openharness.mcp.client import McpClientManager
from openharness.mcp.config import load_mcp_server_configs
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.plugins import load_plugins
from openharness.plugins.types import LoadedPlugin
from openharness.prompts import (
    build_stable_system_prompt_result,
    build_turn_context,
)
from openharness.prompts.active_files import collect_active_files
from openharness.services.agent_backend import (
    AgentBackend,
    LocalPythonBackend,
    SessionRecordingBackend,
)
from openharness.services.agent_run_port import ServiceAgentRunPort
from openharness.services.backends.codex_app_server import CodexAppServerBackend
from openharness.services.compact_port import ServiceCompactPort
from openharness.services.cron_port import ServiceCronPort
from openharness.services.event_pipeline import EventPipeline, create_event_pipeline
from openharness.services.lsp_port import ServiceLspPort
from openharness.services.memory_port import ServiceMemoryPort
from openharness.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend
from openharness.services.session_storage import (
    get_project_session_dir,
    resumable_tool_metadata,
)
from openharness.services.subagent_port import ServiceCodexSubagentPort
from openharness.services.subagent_provider import SubagentManager
from openharness.state import AppState, AppStateStore
from openharness.tools import ToolRegistry, create_default_tool_registry

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[..., Awaitable[str]]
EditApprovalPrompt = Callable[[str, str, int, int], Awaitable[str]]
SystemPrinter = Callable[[str], Awaitable[None]]
StreamRenderer = Callable[[StreamEvent], Awaitable[None]]
ClearHandler = Callable[[], Awaitable[None]]


def _resolve_image_generation_config(settings: Settings) -> dict[str, str]:
    """Resolve image generation configuration from settings, environment, and Codex auth."""
    from openharness.config.settings import ImageGenerationConfig, ProviderProfile

    cfg = settings.image_generation
    env_cfg = ImageGenerationConfig.from_env()
    resolved = {
        "provider": cfg.provider or env_cfg.provider,
        "model": cfg.model or env_cfg.model,
        "api_key": cfg.api_key or env_cfg.api_key,
        "base_url": cfg.base_url or env_cfg.base_url,
        "codex_model": cfg.codex_model or env_cfg.codex_model,
        "codex_base_url": cfg.codex_base_url or env_cfg.codex_base_url,
    }

    try:
        codex_profile = settings.merged_profiles().get("codex") or ProviderProfile(
            label="Codex Subscription",
            provider="openai_codex",
            api_format="openai",
            auth_source="codex_subscription",
            default_model="gpt-5.4",
        )
        codex_settings = settings.model_copy(
            update={
                "active_profile": "codex",
                "profiles": {**settings.profiles, "codex": codex_profile},
            }
        ).materialize_active_profile()
        codex_auth = codex_settings.resolve_auth()
        resolved["codex_auth_token"] = codex_auth.value
        resolved["codex_base_url"] = resolved["codex_base_url"] or (codex_settings.base_url or "")
        resolved["codex_model"] = resolved["codex_model"] or codex_settings.model
    except Exception:
        logger.debug("Codex image-generation auth is unavailable", exc_info=True)

    return resolved


def _codex_permission_mode(mode: str) -> str:
    """Map OpenHarness permission modes onto Codex app-server policies."""
    return {
        "full_auto": "never",
        "plan": "never",
        "default": "approve-for-me",
    }.get(mode, "approve-for-me")


def _resolve_vision_config(settings: Settings) -> dict[str, str]:
    """Resolve the vision model configuration from settings or environment.

    Priority: settings.vision fields > environment variables > empty.
    """
    from openharness.config.settings import VisionModelConfig

    cfg = settings.vision
    if cfg.is_configured:
        return {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
        }

    # Fall back to environment variables
    env_cfg = VisionModelConfig.from_env()
    if env_cfg.is_configured:
        return {
            "model": env_cfg.model,
            "api_key": env_cfg.api_key,
            "base_url": env_cfg.base_url,
        }

    return {}


@dataclass
class RuntimeBundle:
    """Shared runtime objects for one interactive session."""

    api_client: SupportsStreamingMessages
    cwd: str
    mcp_manager: McpClientManager
    tool_registry: ToolRegistry
    app_state: AppStateStore
    hook_executor: HookExecutor
    engine: QueryEngine
    commands: object
    external_api_client: bool
    enforce_max_turns: bool = True
    session_id: str = ""
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    session_backend: SessionBackend = DEFAULT_SESSION_BACKEND
    extra_skill_dirs: tuple[str, ...] = ()
    extra_plugin_roots: tuple[str, ...] = ()
    memory_backend: Any | None = None
    include_project_memory: bool = True
    autodream_context: dict[str, object] | None = None
    event_pipeline: EventPipeline | None = None
    agent_backend: AgentBackend | None = None
    subagent_manager: SubagentManager | None = None

    def backend(self) -> AgentBackend:
        """Return the active agent backend, defaulting to the local engine."""
        return self.agent_backend or LocalPythonBackend(self.engine)

    def current_settings(self) -> Settings:
        """Return the effective settings for this session.

        We persist most settings to disk (``~/.openharness/settings.json``), but
        CLI options like ``--model``/``--api-format`` should remain in effect for
        the lifetime of the running process. Without this overlay, issuing any
        slash command (e.g. ``/fast``) would refresh UI state from disk and
        "snap back" the model/provider to whatever is stored in the config file.
        """
        return load_settings().merge_cli_overrides(**self.settings_overrides)

    def current_plugins(self) -> list[LoadedPlugin]:
        """Return currently visible plugins for the working tree."""
        # ``load_plugins`` is re-exported through a bare module ``__getattr__``
        # (so it types as Any); pin the real return type at the boundary.
        plugins: list[LoadedPlugin] = load_plugins(
            self.current_settings(),
            self.cwd,
            extra_roots=self.extra_plugin_roots,
        )
        return plugins

    def hook_summary(self) -> str:
        """Return the current hook summary."""
        # ``load_hook_registry`` is re-exported through a bare module
        # ``__getattr__`` (so it types as Any); pin the real registry type.
        registry: HookRegistry = load_hook_registry(self.current_settings(), self.current_plugins())
        return registry.summary()

    def plugin_summary(self) -> str:
        """Return the current plugin summary."""
        plugins = self.current_plugins()
        if not plugins:
            return "No plugins discovered."
        lines = ["Plugins:"]
        for plugin in plugins:
            state = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.manifest.name} [{state}] {plugin.manifest.description}")
        return "\n".join(lines)

    def mcp_summary(self) -> str:
        """Return the current MCP summary."""
        statuses = self.mcp_manager.list_statuses()
        if not statuses:
            return "No MCP servers configured."
        lines = ["MCP servers:"]
        for status in statuses:
            suffix = f" - {status.detail}" if status.detail else ""
            lines.append(f"- {status.name}: {status.state}{suffix}")
            if status.tools:
                lines.append(f"  tools: {', '.join(tool.name for tool in status.tools)}")
            if status.resources:
                lines.append(f"  resources: {', '.join(resource.uri for resource in status.resources)}")
        return "\n".join(lines)


def _resolve_api_client_from_settings(settings: Settings) -> SupportsStreamingMessages:
    """Build the appropriate API client for the resolved settings."""
    # Ensure profile fields (base_url, model, api_format) are projected to settings
    settings = settings.materialize_active_profile()

    def _safe_resolve_auth() -> ResolvedAuth:
        try:
            return settings.resolve_auth()
        except Exception as exc:  # noqa: BLE001 - auth failures become a clean SystemExit
            _print_auth_resolution_error(settings, exc)
            raise SystemExit(1)

    if settings.api_format == "copilot":
        from openharness.api.copilot_client import COPILOT_DEFAULT_MODEL

        copilot_model = (
            COPILOT_DEFAULT_MODEL
            if settings.model in {"claude-sonnet-4-20250514", "claude-sonnet-4-6", "sonnet", "default"}
            else settings.model
        )
        return CopilotClient(model=copilot_model)
    if settings.provider == "openai_codex":
        auth = _safe_resolve_auth()
        return CodexApiClient(
            auth_token=auth.value,
            base_url=settings.base_url,
        )
    if settings.provider == "anthropic_claude":
        return AnthropicApiClient(
            auth_token=_safe_resolve_auth().value,
            base_url=settings.base_url,
            claude_oauth=True,
            auth_token_resolver=lambda: settings.resolve_auth().value,
        )
    if settings.api_format in ("openai", "openai_compat"):
        auth = _safe_resolve_auth()
        return OpenAICompatibleClient(
            api_key=auth.value,
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
    auth = _safe_resolve_auth()
    return AnthropicApiClient(
        api_key=auth.value,
        base_url=settings.base_url,
    )


def _print_auth_resolution_error(settings: Settings, exc: Exception) -> None:
    """Render auth failures without collapsing subscription errors into API-key advice."""
    try:
        profile_name, profile = settings.resolve_profile()
        auth_source = (getattr(profile, "auth_source", "") or "").strip()
    except (ValueError, KeyError, TypeError):
        profile_name = ""
        auth_source = ""

    message = str(exc).strip() or exc.__class__.__name__
    if auth_source in {"claude_subscription", "codex_subscription"}:
        login_command = "claude-login" if auth_source == "claude_subscription" else "codex-login"
        provider_name = profile_name or (
            "claude-subscription" if auth_source == "claude_subscription" else "codex"
        )
        print(
            f"Error: {message}\n"
            f"  This profile uses subscription auth, not an API key.\n"
            f"  Run `oh auth {login_command}` to bind the local CLI session, then\n"
            f"  run `oh provider use {provider_name}` to activate it.",
            file=sys.stderr,
        )
        return

    print(
        "Error: No API key configured.\n"
        f"  {message}\n"
        "  Run `oh auth login` to set up authentication, or set the\n"
        "  ANTHROPIC_API_KEY (or OPENAI_API_KEY) environment variable.",
        file=sys.stderr,
    )


async def build_runtime(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_prompt: PermissionPrompt | None = None,
    ask_user_prompt: AskUserPrompt | None = None,
    edit_approval_prompt: EditApprovalPrompt | None = None,
    restore_messages: list[dict[str, Any]] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    enforce_max_turns: bool = True,
    session_backend: SessionBackend | None = None,
    permission_mode: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    memory_backend: Any | None = None,
    include_project_memory: bool = True,
    autodream_context: dict[str, object] | None = None,
    command_registry_factory: Callable[..., object] | None = None,
) -> RuntimeBundle:
    """Build the shared runtime for an OpenHarness session."""
    settings_overrides: dict[str, Any] = {
        "model": model,
        "max_turns": max_turns,
        "effort": effort,
        "personality": personality,
        "tool_profile": tool_profile,
        "base_url": base_url,
        "system_prompt": system_prompt,
        "api_key": api_key,
        "api_format": api_format,
        "active_profile": active_profile,
        "permission_mode": permission_mode,
    }
    settings = load_settings().merge_cli_overrides(**settings_overrides)
    cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(Path.cwd())
    normalized_skill_dirs = tuple(str(Path(path).expanduser().resolve()) for path in (extra_skill_dirs or ()))
    normalized_plugin_roots = tuple(str(Path(path).expanduser().resolve()) for path in (extra_plugin_roots or ()))
    plugins = load_plugins(settings, cwd, extra_roots=normalized_plugin_roots)
    if api_client:
        resolved_api_client = api_client
    else:
        resolved_api_client = _resolve_api_client_from_settings(settings)
    mcp_manager = McpClientManager(load_mcp_server_configs(settings, plugins))
    await mcp_manager.connect_all()
    tool_registry = create_default_tool_registry(
        mcp_manager,
        include_codex_subagent=settings.codex_subagent_enabled,
    )
    # Register plugin-provided tools in a deterministic order so the wire
    # schema matches across sessions.
    for plugin in sorted(plugins, key=lambda item: item.name):
        if plugin.enabled and plugin.tools:
            for tool in sorted(plugin.tools, key=lambda item: item.name):
                tool_registry.register(tool)
    provider = detect_provider(settings)
    bridge_manager = get_bridge_manager()
    app_state = AppStateStore(
        AppState(
            # Show the effective runtime model (after CLI/env/profile merges),
            # not profile.last_model which may be stale.
            model=settings.model,
            permission_mode=settings.permission.mode.value,
            theme=settings.theme,
            todo_panel=settings.todo_panel,
            cwd=cwd,
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
            mcp_connected=sum(1 for status in mcp_manager.list_statuses() if status.state == "connected"),
            mcp_failed=sum(1 for status in mcp_manager.list_statuses() if status.state == "failed"),
            bridge_sessions=len(bridge_manager.list_sessions()),
            output_style=settings.output_style,
            keybindings=load_keybindings(),
        )
    )
    hook_reloader = HookReloader(get_config_file_path())
    hook_executor = HookExecutor(
        hook_reloader.current_registry() if api_client is None else load_hook_registry(settings, plugins),
        HookExecutionContext(
            cwd=Path(cwd).resolve(),
            api_client=resolved_api_client,
            default_model=settings.model,
        ),
    )
    engine_max_turns = settings.max_turns if (enforce_max_turns or max_turns is not None) else None
    # The system prompt is built once and kept byte-stable for the session.
    # Per-turn context (permission mode, effort, memory, active files, live
    # environment) is attached to the latest user message instead.
    prompt_result = build_stable_system_prompt_result(
        settings,
        cwd=cwd,
        extra_skill_dirs=normalized_skill_dirs,
        extra_plugin_roots=normalized_plugin_roots,
        available_tools={tool.name for tool in tool_registry.list_tools()},
    )
    system_prompt_text = prompt_result.text
    from uuid import uuid4

    session_id = uuid4().hex[:12]

    restored_metadata: dict[str, object] = {
        "permission_mode": settings.permission.mode.value,
        # Plan checklists are session state, not the repository's TODO.md.
        "plan_file": str(get_project_session_dir(cwd) / f"plan-{session_id}.md"),
        "read_file_state": [],
        "invoked_skills": [],
        "async_agent_state": [],
        "async_agent_tasks": [],
        "recent_work_log": [],
        "recent_verified_work": [],
        "task_focus_state": {
            "goal": "",
            "recent_goals": [],
            "active_artifacts": [],
            "verified_state": [],
            "next_step": "",
        },
        "compact_checkpoints": [],
        # Empty on purpose: the ledger has to be created lazily so a resumed
        # session inherits the persisted one instead of overwriting it.
        "verification_ledger": {},
    }
    # Only the persisted work state (read files, work log, focus) is restored.
    # A stale ``permission_mode`` must not override the live checker's mode.
    restored_metadata.update(resumable_tool_metadata(restore_tool_metadata))
    # Config, not state: re-injected from settings on every start so a changed
    # setting takes effect immediately and is never persisted into the session.
    restored_metadata["verification_gate"] = {
        "enabled": settings.verification.enabled,
        "nudge_limit": settings.verification.nudge_limit,
        "require_full_suite": settings.verification.require_full_suite,
        "hygiene_check": settings.verification.hygiene_check,
    }

    event_pipeline = await create_event_pipeline(cwd, session_id)
    try:
        session_projection = event_pipeline.projections.get("session")
    except KeyError:
        session_projection = None
    session_manager = SessionManager(
        session_id=session_id,
        event_log=event_pipeline.store,
        message_view=(
            (lambda: list(session_projection.derive_messages()))  # type: ignore[attr-defined]
            if session_projection is not None
            else None
        ),
        usage_view=(
            (lambda: session_projection.derive_usage())  # type: ignore[attr-defined]
            if session_projection is not None
            else None
        ),
    )
    if not restore_messages:
        await session_manager.restore_from_event_log()

    def _current_settings() -> Settings:
        return load_settings().merge_cli_overrides(**settings_overrides)

    def _build_turn_context(
        user_prompt: str,
        tool_metadata: dict[str, object],
    ) -> str:
        """Build the dynamic context attached to the latest user message."""
        current = _current_settings()
        active = collect_active_files(user_prompt, tool_metadata)
        return build_turn_context(
            current,
            cwd=cwd,
            latest_user_prompt=user_prompt,
            include_project_memory=include_project_memory,
            active_files=active,
        )

    engine = QueryEngine(
        api_client=resolved_api_client,
        tool_registry=tool_registry,
        permission_checker=PermissionChecker(settings.permission),
        cwd=cwd,
        model=settings.model,
        system_prompt=system_prompt_text,
        system_prompt_cacheable_prefix=prompt_result.cacheable_prefix,
        system_prompt_section_hashes=prompt_result.section_hashes,
        max_tokens=settings.max_tokens,
        context_window_tokens=settings.context_window_tokens or settings.memory.context_window_tokens,
        auto_compact_threshold_tokens=(
            settings.auto_compact_threshold_tokens
            or settings.memory.auto_compact_threshold_tokens
        ),
        max_turns=engine_max_turns,
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        hook_executor=hook_executor,
        memory_port=ServiceMemoryPort(),
        compact_port=ServiceCompactPort(),
        settings=settings,
        event_sink=event_pipeline.sink,
        event_sequencer=event_pipeline.sequencer,
        session_id=session_id,
        session_manager=session_manager,
        turn_context_builder=_build_turn_context,
        warmup_wait_seconds=settings.cache_warmup_wait_seconds,
        tool_metadata={
            "mcp_manager": mcp_manager,
            "bridge_manager": bridge_manager,
            "agent_run_port": ServiceAgentRunPort(),
            "cron_port": ServiceCronPort(),
            "lsp_port": ServiceLspPort(),
            "codex_subagent_port": ServiceCodexSubagentPort(),
            "extra_skill_dirs": normalized_skill_dirs,
            "extra_plugin_roots": normalized_plugin_roots,
            "session_id": session_id,
            "edit_approval_prompt": edit_approval_prompt,
            "vision_model_config": _resolve_vision_config(settings),
            "image_generation_config": _resolve_image_generation_config(settings),
            **restored_metadata,
        },
    )

    # Warmup is opt-in for explicit-cache providers and automatic for
    # implicit-cache providers (DeepSeek/OpenAI).  It runs in the background so
    # the first user request is never blocked on it.
    warmup_enabled = settings.cache_warmup_enabled
    if warmup_enabled is None:
        warmup_enabled = supports_implicit_prompt_cache(provider)
    if warmup_enabled:
        engine.set_cache_warmup(
            get_default_cache_warmup(ttl_seconds=settings.cache_warmup_ttl_seconds)
        )
        warmup_task = asyncio.create_task(engine.warmup_cache())
        engine.set_warmup_task(warmup_task)
        _BACKGROUND_TASKS.add(warmup_task)
        warmup_task.add_done_callback(_BACKGROUND_TASKS.discard)
        # Optional idle/long-tool keepalive.  Off by default: it spends tokens
        # on a schedule the provider TTL does not disclose.
        if settings.cache_refresh_enabled:
            keepalive_task = engine.start_cache_refresh(
                settings.cache_refresh_idle_seconds
            )
            if keepalive_task is not None:
                _BACKGROUND_TASKS.add(keepalive_task)
                keepalive_task.add_done_callback(_BACKGROUND_TASKS.discard)

    def _set_permission_mode(mode: str) -> None:
        """Apply a plan-mode switch to disk, the live checker and the prompt."""
        permission_mode = PermissionMode(mode)
        disk_settings = load_settings()
        disk_settings.permission.set_mode(permission_mode)
        save_settings(disk_settings)
        settings_overrides.pop("permission_mode", None)
        # Mutate the checker held by the current turn so the remaining tool
        # calls stop being blocked immediately.
        engine.set_permission_mode(permission_mode)
        app_state.set(permission_mode=permission_mode.value)
        # The system prompt is frozen; only the turn context changes.
        engine.refresh_turn_context()

    engine.tool_metadata["set_permission_mode"] = _set_permission_mode

    if autodream_context is not None:
        engine.tool_metadata["autodream_context"] = autodream_context
    # Restore messages from a saved session if provided
    if restore_messages and not session_manager.messages:
        restored = sanitize_conversation_messages(
            [ConversationMessage.model_validate(m) for m in restore_messages]
        )
        engine.load_messages(restored)
        await event_pipeline.seed_messages(restored)

    # Start Docker sandbox if configured
    if settings.sandbox.enabled and settings.sandbox.backend == "docker":
        from openharness.sandbox.session import start_docker_sandbox

        await start_docker_sandbox(settings, session_id, Path(cwd))

    agent_backend: AgentBackend = LocalPythonBackend(engine)
    if settings.agent_backend.strip().lower() == "codex":
        agent_backend = SessionRecordingBackend(
            CodexAppServerBackend(
                cwd=str(cwd),
                permission_mode=_codex_permission_mode(
                    settings.permission.mode.value
                ),
                model=settings.model,
            ),
            session_manager,
            event_sink=event_pipeline.sink,
            event_sequencer=event_pipeline.sequencer,
            session_id=session_id,
        )

    commands = (
        command_registry_factory(
            plugin_commands=[
                command
                for plugin in plugins
                if plugin.enabled
                for command in plugin.commands
            ]
        )
        if command_registry_factory is not None
        else None
    )

    return RuntimeBundle(
        api_client=resolved_api_client,
        cwd=cwd,
        mcp_manager=mcp_manager,
        tool_registry=tool_registry,
        app_state=app_state,
        hook_executor=hook_executor,
        engine=engine,
        commands=commands,
        external_api_client=api_client is not None,
        enforce_max_turns=enforce_max_turns or max_turns is not None,
        session_id=session_id,
        settings_overrides=settings_overrides,
        session_backend=session_backend or DEFAULT_SESSION_BACKEND,
        extra_skill_dirs=normalized_skill_dirs,
        extra_plugin_roots=normalized_plugin_roots,
        memory_backend=memory_backend,
        include_project_memory=include_project_memory,
        autodream_context=autodream_context,
        event_pipeline=event_pipeline,
        agent_backend=agent_backend,
        subagent_manager=SubagentManager(),
    )


async def start_runtime(bundle: RuntimeBundle) -> None:
    """Run session start hooks."""
    await bundle.hook_executor.execute(
        HookEvent.SESSION_START,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_START.value},
    )


async def close_runtime(bundle: RuntimeBundle) -> None:
    """Close runtime-owned resources."""
    from openharness.sandbox.session import stop_docker_sandbox

    await stop_docker_sandbox()
    # Stop scheduled cache refreshes before the API client closes.
    keepalive_task = bundle.engine.stop_cache_refresh()
    if keepalive_task is not None:
        await asyncio.gather(keepalive_task, return_exceptions=True)
    # Stop task-log followers before the transcript backend goes away.
    from openharness.services.task_follow import stop_all_task_followers

    await stop_all_task_followers()
    # Extract local environment rules from session before closing
    try:
        from openharness.personalization.session_hook import update_rules_from_session
        update_rules_from_session(bundle.engine.messages)
    except Exception:
        logger.debug(
            "Failed to update personalization rules from session",
            exc_info=True,
        )  # best-effort; never block session end

    await bundle.mcp_manager.close()
    if bundle.event_pipeline is not None:
        await bundle.event_pipeline.close()
    if bundle.subagent_manager is not None:
        await bundle.subagent_manager.close()
    await bundle.hook_executor.execute(
        HookEvent.SESSION_END,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_END.value},
    )
    close_api_client = getattr(bundle.api_client, "close", None)
    if close_api_client is not None:
        await close_api_client()
    # Let pending transport-connection-lost callbacks run while the loop is
    # still alive. On Windows this avoids reset noise during loop teardown.
    await asyncio.sleep(0)




def _last_user_text(messages: list[ConversationMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and msg.text.strip():
            return msg.text.strip()
    return ""


