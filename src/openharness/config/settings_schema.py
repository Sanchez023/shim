"""Settings schema: Pydantic models and the Settings root."""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, Field

from openharness.config.provider_types import ProviderProfile, ResolvedAuth
from openharness.config.providers import (
    _profile_from_flat_settings,
    auth_source_provider_name,
    credential_storage_provider_name,
    default_auth_source_for_provider,
    default_provider_profiles,
    resolve_auth_env_value,
    resolve_model_setting,
)
from openharness.hooks.schemas import HookDefinition
from openharness.mcp.types import McpServerConfig
from openharness.permissions.modes import PermissionMode

_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

def strip_ansi_escape_sequences(text: str) -> str:
    """Remove ANSI escape sequences from text.

    This is used to clean environment variables that may contain terminal
    formatting codes (e.g., '[1m' for bold) which can corrupt API requests.
    """
    if not text:
        return text
    return _ANSI_ESCAPE_PATTERN.sub("", text)


class PathRuleConfig(BaseModel):
    """A glob-pattern path permission rule."""

    pattern: str
    allow: bool = True


class PermissionSettings(BaseModel):
    """Permission mode configuration."""

    mode: PermissionMode = PermissionMode.DEFAULT
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    path_rules: list[PathRuleConfig] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)

    def set_mode(self, mode: PermissionMode | str) -> None:
        """Single mutation point for the persisted permission mode."""
        self.mode = mode if isinstance(mode, PermissionMode) else PermissionMode(mode)


class MemorySettings(BaseModel):
    """Memory system configuration."""

    enabled: bool = True
    max_files: int = 5
    max_entrypoint_lines: int = 200
    max_entrypoint_bytes: int = 25_000
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    auto_extract_enabled: bool = False
    auto_extract_max_records: int = 3
    session_memory_enabled: bool = True
    auto_dream_enabled: bool = False
    auto_dream_min_hours: float = 24.0
    auto_dream_min_sessions: int = 5


class SandboxNetworkSettings(BaseModel):
    """OS-level network restrictions passed to sandbox-runtime."""

    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)


class SandboxFilesystemSettings(BaseModel):
    """OS-level filesystem restrictions passed to sandbox-runtime."""

    allow_read: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=lambda: ["."])
    deny_write: list[str] = Field(default_factory=list)


class DockerSandboxSettings(BaseModel):
    """Docker-specific sandbox configuration."""

    image: str = "openharness-sandbox:latest"
    auto_build_image: bool = True
    cpu_limit: float = 0.0
    memory_limit: str = ""
    extra_mounts: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class SandboxSettings(BaseModel):
    """Sandbox-runtime integration settings."""

    enabled: bool = False
    backend: str = "srt"
    #: What to do when ``enabled`` is true but the runtime cannot be reached.
    #: ``deny`` refuses to run instead of silently falling back to the host
    #: (ADR-0012 D5); ``warn`` is the old degrade-and-log behaviour, now an
    #: explicit choice rather than the default.
    unavailable_policy: str = "deny"
    #: Legacy switch.  ``None`` means "follow ``unavailable_policy``".
    fail_if_unavailable: bool | None = None
    enabled_platforms: list[str] = Field(default_factory=list)
    network: SandboxNetworkSettings = Field(default_factory=SandboxNetworkSettings)
    filesystem: SandboxFilesystemSettings = Field(default_factory=SandboxFilesystemSettings)
    docker: DockerSandboxSettings = Field(default_factory=DockerSandboxSettings)

    @property
    def refuses_unavailable(self) -> bool:
        """True when an unavailable sandbox must fail instead of degrading."""
        if self.fail_if_unavailable is not None:
            return self.fail_if_unavailable
        return self.unavailable_policy != "warn"


class WebSettings(BaseModel):
    """Outbound web tool configuration."""

    proxy: str | None = None
    resolution_mode: str = "auto"
    synthetic_dns_cidrs: list[str] = Field(default_factory=list)
    search_url: str | None = None


class NotificationSettings(BaseModel):
    """Desktop notification behavior for completed turns."""

    enabled: bool = True
    on_turn_complete: bool = True
    min_duration_seconds: float = 0.0


class VerificationSettings(BaseModel):
    """Completion gate: was the last edit verified before the turn ended?

    The engine keeps a ledger of edits and verification runs (see
    ``engine/verification_ledger.py``).  When the ledger says the work is
    unverified, the turn gets one reminder instead of silently reporting
    success.  ``nudge_limit`` is per unverified streak and resets on any
    workspace-wide green run, so a long session cannot be nagged twice for the
    same omission.
    """

    enabled: bool = True
    nudge_limit: int = Field(default=1, ge=0, le=3)
    #: Only a run that covers the whole workspace closes the gate.  Turning this
    #: off treats a partial green run as closure, which is almost never right.
    require_full_suite: bool = True
    #: Warn when the session leaves worktrees, stashes or untracked paths behind.
    hygiene_check: bool = True


class ImageGenerationConfig(BaseModel):
    """Configuration for the image_generation tool."""

    provider: str = "auto"
    model: str = "gpt-image-2"
    api_key: str = ""
    base_url: str = ""
    codex_model: str = "gpt-5.4"
    codex_base_url: str = ""

    @classmethod
    def from_env(cls) -> ImageGenerationConfig:
        """Load image generation config from environment variables."""
        return cls(
            provider=os.environ.get("OPENHARNESS_IMAGE_GENERATION_PROVIDER", "auto").strip()
            or "auto",
            model=os.environ.get("OPENHARNESS_IMAGE_GENERATION_MODEL", "gpt-image-2").strip()
            or "gpt-image-2",
            api_key=os.environ.get("OPENHARNESS_IMAGE_GENERATION_API_KEY", "").strip(),
            base_url=os.environ.get("OPENHARNESS_IMAGE_GENERATION_BASE_URL", "").strip(),
            codex_model=os.environ.get("OPENHARNESS_IMAGE_GENERATION_CODEX_MODEL", "gpt-5.4").strip()
            or "gpt-5.4",
            codex_base_url=os.environ.get("OPENHARNESS_IMAGE_GENERATION_CODEX_BASE_URL", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """Return True when either a key provider or Codex provider is selected."""
        return bool(self.api_key or self.provider in {"auto", "codex"})


class VisionModelConfig(BaseModel):
    """Configuration for the vision model used by the image_to_text tool.

    When the active model does not support multimodal input, the agent loop
    automatically falls back to this vision model to describe images.
    """

    model: str = ""
    api_key: str = ""
    base_url: str = ""

    @classmethod
    def from_env(cls) -> VisionModelConfig:
        """Load vision model config from environment variables."""
        return cls(
            model=os.environ.get("OPENHARNESS_VISION_MODEL", "").strip(),
            api_key=os.environ.get("OPENHARNESS_VISION_API_KEY", "").strip(),
            base_url=os.environ.get("OPENHARNESS_VISION_BASE_URL", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """Return True when both model and api_key are set."""
        return bool(self.model and self.api_key)


class Settings(BaseModel):
    """Main settings model for OpenHarness."""

    # API configuration
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 16384
    base_url: str | None = None
    timeout: float = 30.0
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    api_format: str = "anthropic"  # "anthropic", "openai", or "copilot"
    provider: str = ""
    active_profile: str = "claude-api"
    profiles: dict[str, ProviderProfile] = Field(default_factory=default_provider_profiles)
    max_turns: int = 200

    # Agent runtime
    agent_backend: str = "local"  # "local" or "codex"
    codex_subagent_enabled: bool = False

    # Prompt-cache behavior.  ``None`` means "auto": warm once at session
    # startup for providers with implicit prefix caching (DeepSeek/OpenAI).
    cache_warmup_enabled: bool | None = None
    cache_warmup_ttl_seconds: float = 30 * 60
    # Seconds the first turn may wait for the background warmup to finish.
    cache_warmup_wait_seconds: float = 2.0
    # Optional keepalive: refresh the prefix when the session has been idle
    # longer than ``cache_refresh_idle_seconds``.  Off by default because it
    # spends tokens on a schedule the provider does not disclose.
    cache_refresh_enabled: bool = False
    cache_refresh_idle_seconds: float = 10 * 60
    # A single message this large is almost always a pasted document or log.
    # Save it to an artifact and send a preview instead of paying a full
    # prompt-cache miss for every pasted token.
    offload_large_pastes: bool = True
    large_paste_threshold_chars: int = 24_000
    large_paste_preview_chars: int = 2_000

    # Behavior
    system_prompt: str | None = None
    permission: PermissionSettings = Field(default_factory=PermissionSettings)
    hooks: dict[str, list[HookDefinition]] = Field(default_factory=dict)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    enabled_plugins: dict[str, bool] = Field(default_factory=dict)
    allow_project_plugins: bool = False
    allow_project_skills: bool = True
    project_skill_dirs: list[str] = Field(
        default_factory=lambda: [".openharness/skills", ".agents/skills", ".claude/skills"]
    )
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    # UI
    theme: str = "default"
    # TUI todo/plan panel shape: full | compact | hidden (ctrl+t cycles it).
    todo_panel: str = "full"
    output_style: str = "default"
    vim_mode: bool = False
    voice_mode: bool = False
    fast_mode: bool = False
    effort: str = "medium"
    passes: int = 1
    personality: str = "shim"
    personality_custom: str = ""
    tool_profile: str = "core"
    verbose: bool = False

    # Desktop notifications
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)

    # Vision model (image-to-text fallback)
    vision: VisionModelConfig = Field(default_factory=VisionModelConfig)

    # Image generation model
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)

    def merged_profiles(self) -> dict[str, ProviderProfile]:
        """Return the saved profiles merged over the built-in catalog."""
        merged = default_provider_profiles()
        for name, raw_profile in self.profiles.items():
            profile = (
                raw_profile.model_copy(deep=True)
                if isinstance(raw_profile, ProviderProfile)
                else ProviderProfile.model_validate(raw_profile)
            )
            builtin = merged.get(name)
            if builtin is not None and profile.base_url is None and builtin.base_url is not None:
                profile = profile.model_copy(update={"base_url": builtin.base_url})
            merged[name] = profile
        return merged

    def resolve_profile(self, name: str | None = None) -> tuple[str, ProviderProfile]:
        """Return the active provider profile."""
        profiles = self.merged_profiles()
        profile_name = (name or self.active_profile or os.environ.get("OPENHARNESS_PROFILE") or "").strip() or "claude-api"
        if profile_name not in profiles:
            fallback_name, fallback = _profile_from_flat_settings(self)
            profiles[fallback_name] = fallback
            profile_name = fallback_name
        return profile_name, profiles[profile_name].model_copy(deep=True)

    def materialize_active_profile(self) -> Settings:
        """Project the active profile back onto legacy flat settings fields."""
        profile_name, profile = self.resolve_profile()
        configured_model = (profile.last_model or "").strip() or profile.default_model
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": self.merged_profiles(),
                "provider": profile.provider,
                "api_format": profile.api_format,
                "base_url": profile.base_url,
                "context_window_tokens": profile.context_window_tokens,
                "auto_compact_threshold_tokens": profile.auto_compact_threshold_tokens,
                "model": resolve_model_setting(
                    configured_model,
                    profile.provider,
                    default_model=profile.default_model,
                    permission_mode=self.permission.mode.value,
                ),
            }
        )

    def sync_active_profile_from_flat_fields(self) -> Settings:
        """Fold legacy flat provider fields back into the active profile.

        This preserves compatibility for callers that still construct `Settings`
        by setting top-level `provider` / `api_format` / `base_url` / `model`
        directly before the profile layer is used everywhere.
        """
        profile_name, profile = self.resolve_profile()
        profile_from_env = bool(os.environ.get("OPENHARNESS_PROFILE"))
        flat_profile_fields_match_profile = profile_from_env or (
            (self.provider or "").strip() == profile.provider
            and (self.api_format or "").strip() == profile.api_format
            and self.base_url == profile.base_url
        )
        next_provider = profile.provider if flat_profile_fields_match_profile else (self.provider or "").strip() or profile.provider
        next_api_format = profile.api_format if flat_profile_fields_match_profile else (self.api_format or "").strip() or profile.api_format
        next_base_url = profile.base_url if flat_profile_fields_match_profile else (self.base_url if self.base_url is not None else profile.base_url)
        next_context_window_tokens = (
            self.context_window_tokens
            if self.context_window_tokens is not None
            else profile.context_window_tokens
        )
        next_auto_compact_threshold_tokens = (
            self.auto_compact_threshold_tokens
            if self.auto_compact_threshold_tokens is not None
            else profile.auto_compact_threshold_tokens
        )
        flat_model = (self.model or "").strip()
        resolved_profile_model = resolve_model_setting(
            (profile.last_model or "").strip() or profile.default_model,
            profile.provider,
            default_model=profile.default_model,
            permission_mode=self.permission.mode.value,
        )
        if flat_model and flat_model != resolved_profile_model:
            next_model = flat_model
        else:
            next_model = profile.last_model
        current_default_auth = default_auth_source_for_provider(profile.provider, profile.api_format)
        next_auth_source = profile.auth_source
        if not next_auth_source or next_auth_source == current_default_auth:
            next_auth_source = default_auth_source_for_provider(next_provider, next_api_format)

        updated_profile = profile.model_copy(
            update={
                "provider": next_provider,
                "api_format": next_api_format,
                "base_url": next_base_url,
                "auth_source": next_auth_source,
                "last_model": next_model,
                "context_window_tokens": next_context_window_tokens,
                "auto_compact_threshold_tokens": next_auto_compact_threshold_tokens,
            }
        )
        profiles = self.merged_profiles()
        profiles[profile_name] = updated_profile
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": profiles,
            }
        )

    def resolve_api_key(self) -> str:
        """Resolve API key with precedence: instance value > env var > empty.

        For ``copilot`` api_format the key is managed separately via
        ``oh auth copilot-login`` and this method is not called.

        Returns the API key string. Raises ValueError if no key is found.
        """
        profile_name, profile = self.resolve_profile()
        del profile_name
        if profile.provider == "openai_codex":
            return self.resolve_auth().value
        if profile.provider == "anthropic_claude":
            raise ValueError(
                "Current provider uses Anthropic auth tokens instead of API keys. "
                "Use resolve_auth() for runtime credential resolution."
            )
        # Copilot format manages its own auth; skip normal key resolution.
        if profile.api_format == "copilot":
            return "copilot-managed"

        if self.api_key:
            return self.api_key

        env_resolved = resolve_auth_env_value(profile.auth_source)
        if env_resolved:
            _, env_value = env_resolved
            return env_value

        raise ValueError(
            "No API key found. Set an OPENHARNESS_* provider API key "
            "(preferred) or the matching native provider environment variable, "
            "or configure api_key in ~/.openharness/settings.json"
        )

    def resolve_auth(self) -> ResolvedAuth:
        """Resolve auth for the current provider, including subscription bridges."""
        profile_name, profile = self.resolve_profile()
        provider = profile.provider.strip()
        auth_source = profile.auth_source.strip() or default_auth_source_for_provider(provider, profile.api_format)
        if auth_source in {"codex_subscription", "claude_subscription"}:
            env_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
            if auth_source == "claude_subscription" and env_auth_token:
                return ResolvedAuth(
                    provider=provider,
                    auth_kind="oauth",
                    value=env_auth_token,
                    source="env:ANTHROPIC_AUTH_TOKEN",
                    state="configured",
                )
            from openharness.auth.external import (
                is_third_party_anthropic_endpoint,
                load_external_credential,
            )
            from openharness.auth.storage import load_external_binding

            if auth_source == "claude_subscription" and is_third_party_anthropic_endpoint(profile.base_url):
                raise ValueError(
                    "Claude subscription auth only supports direct Anthropic/Claude endpoints. "
                    "Use an API-key-backed Anthropic-compatible profile for third-party base URLs."
                )
            binding = load_external_binding(auth_source_provider_name(auth_source))
            if binding is None:
                raise ValueError(
                    f"No external auth binding found for {auth_source}. Run 'oh auth "
                    f"{'codex-login' if auth_source == 'codex_subscription' else 'claude-login'}' first."
                )
            credential = load_external_credential(
                binding,
                refresh_if_needed=(auth_source == "claude_subscription"),
            )
            return ResolvedAuth(
                provider=provider,
                auth_kind=credential.auth_kind,
                value=credential.value,
                source=f"external:{credential.source_path}",
                state="configured",
            )

        if auth_source == "copilot_oauth":
            return ResolvedAuth(
                provider="copilot",
                auth_kind="oauth_device",
                value="copilot-managed",
                source="copilot",
                state="configured",
            )

        storage_provider = auth_source_provider_name(auth_source)

        from openharness.auth.storage import load_credential

        if profile.credential_slot:
            scoped_storage_provider = f"profile:{profile.credential_slot}"
            scoped = load_credential(scoped_storage_provider, "api_key", use_keyring=False)
            if scoped is None:
                scoped = load_credential(scoped_storage_provider, "api_key")
            if scoped:
                return ResolvedAuth(
                    provider=provider or auth_source_provider_name(auth_source),
                    auth_kind="api_key",
                    value=scoped,
                    source=f"file:{scoped_storage_provider}",
                    state="configured",
                )

        storage_provider = credential_storage_provider_name(profile_name, profile)

        env_resolved = resolve_auth_env_value(auth_source)
        if env_resolved:
            env_var, env_value = env_resolved
            return ResolvedAuth(
                provider=provider or storage_provider,
                auth_kind="api_key",
                value=env_value,
                source=f"env:{env_var}",
                state="configured",
            )

        explicit_key = "" if profile.credential_slot else self.api_key
        if explicit_key:
            return ResolvedAuth(
                provider=provider or storage_provider,
                auth_kind="api_key",
                value=explicit_key,
                source="settings_or_env",
                state="configured",
            )

        stored = load_credential(storage_provider, "api_key")
        if stored:
            return ResolvedAuth(
                provider=provider or auth_source_provider_name(auth_source),
                auth_kind="api_key",
                value=stored,
                source=f"file:{storage_provider}",
                state="configured",
            )

        raise ValueError(
            f"No credentials found for auth source '{auth_source}'. "
            "Configure the matching provider or environment variable first."
        )

    def merge_cli_overrides(self, **overrides: Any) -> Settings:
        """Return a new Settings with CLI overrides applied (non-None values only)."""
        updates = {k: v for k, v in overrides.items() if v is not None}
        permission_mode = updates.pop("permission_mode", None)

        def apply_permission_mode(settings: Settings) -> Settings:
            if permission_mode is None:
                return settings
            return settings.model_copy(
                update={
                    "permission": settings.permission.model_copy(
                        update={"mode": PermissionMode(str(permission_mode))}
                    )
                }
            )

        # Strip ANSI escape sequences from model name if present
        if "model" in updates and isinstance(updates["model"], str):
            updates["model"] = strip_ansi_escape_sequences(updates["model"])
        if "effort" in updates and isinstance(updates["effort"], str):
            effort = updates["effort"].strip().lower()
            if effort in {"none", "disable", "disabled"}:
                effort = "off"
            updates["effort"] = "xhigh" if effort == "max" else effort
        if "personality" in updates and isinstance(updates["personality"], str):
            personality = updates["personality"].strip().lower()
            if personality in {"none", "disable", "disabled"}:
                personality = "off"
            updates["personality"] = personality
        if "tool_profile" in updates and isinstance(updates["tool_profile"], str):
            tool_profile = updates["tool_profile"].strip().lower()
            updates["tool_profile"] = "full" if tool_profile in {"all", "full"} else "core"
        merged = apply_permission_mode(self.model_copy(update=updates))
        if not updates:
            return merged
        profile_keys = {
            "model",
            "base_url",
            "api_format",
            "provider",
            "api_key",
            "active_profile",
            "profiles",
            "context_window_tokens",
            "auto_compact_threshold_tokens",
        }
        profile_updates = profile_keys.intersection(updates)
        if not profile_updates:
            return merged
        if "active_profile" in profile_updates:
            switch_updates = {
                key: value
                for key, value in updates.items()
                if key not in profile_keys or key in {"active_profile", "profiles"}
            }
            switched = apply_permission_mode(self.model_copy(update=switch_updates)).materialize_active_profile()
            remaining_profile_updates = {
                key: value
                for key, value in updates.items()
                if key in profile_keys and key not in {"active_profile", "profiles"}
            }
            if not remaining_profile_updates:
                return switched
            return (
                switched.model_copy(update=remaining_profile_updates)
                .sync_active_profile_from_flat_fields()
                .materialize_active_profile()
            )
        return merged.sync_active_profile_from_flat_fields().materialize_active_profile()
