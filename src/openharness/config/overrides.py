"""Environment-variable overrides for settings."""

from __future__ import annotations

import os
from typing import Any

from openharness.config.providers import (
    default_auth_source_for_provider,
    resolve_auth_env_value,
)
from openharness.config.settings_schema import Settings, strip_ansi_escape_sequences


def _apply_env_overrides(settings: Settings) -> Settings:
    """Apply supported environment variable overrides over loaded settings.

    Provider-scoped env vars (``ANTHROPIC_BASE_URL``, ``ANTHROPIC_MODEL``,
    ``OPENAI_BASE_URL``) only apply when the active profile does *not*
    explicitly configure the corresponding field.  ``OPENHARNESS_*`` env vars
    always override (explicit user intent).
    """
    updates: dict[str, Any] = {}

    # Resolve the active profile to check for explicit settings.
    _, active_profile = settings.resolve_profile()
    profile_has_base_url = active_profile.base_url is not None
    profile_explicit_model = (active_profile.last_model or "").strip()
    profile_has_explicit_model = bool(profile_explicit_model) and profile_explicit_model.lower() not in {"", "default"}

    # --- model ---
    openharness_model = os.environ.get("OPENHARNESS_MODEL")
    if openharness_model:
        updates["model"] = strip_ansi_escape_sequences(openharness_model)
    elif not profile_has_explicit_model:
        anthropic_model = os.environ.get("ANTHROPIC_MODEL")
        if anthropic_model:
            updates["model"] = strip_ansi_escape_sequences(anthropic_model)

    # --- base_url ---
    openharness_base = os.environ.get("OPENHARNESS_BASE_URL")
    if openharness_base:
        updates["base_url"] = openharness_base
    elif not profile_has_base_url:
        generic_base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if generic_base:
            updates["base_url"] = generic_base

    max_tokens = os.environ.get("OPENHARNESS_MAX_TOKENS")
    if max_tokens:
        updates["max_tokens"] = int(max_tokens)

    timeout = os.environ.get("OPENHARNESS_TIMEOUT")
    if timeout:
        updates["timeout"] = float(timeout)

    max_turns = os.environ.get("OPENHARNESS_MAX_TURNS")
    if max_turns:
        updates["max_turns"] = int(max_turns)

    agent_backend = os.environ.get("OPENHARNESS_AGENT_BACKEND")
    if agent_backend:
        updates["agent_backend"] = agent_backend.strip().lower()
    codex_subagent = os.environ.get("OPENHARNESS_CODEX_SUBAGENT_ENABLED")
    if codex_subagent is not None:
        updates["codex_subagent_enabled"] = _parse_bool_env(codex_subagent)

    context_window_tokens = os.environ.get("OPENHARNESS_CONTEXT_WINDOW_TOKENS")
    if context_window_tokens:
        updates["context_window_tokens"] = int(context_window_tokens)

    auto_compact_threshold_tokens = os.environ.get("OPENHARNESS_AUTO_COMPACT_THRESHOLD_TOKENS")
    if auto_compact_threshold_tokens:
        updates["auto_compact_threshold_tokens"] = int(auto_compact_threshold_tokens)

    cache_warmup = os.environ.get("OPENHARNESS_CACHE_WARMUP")
    if cache_warmup is not None and cache_warmup.strip():
        updates["cache_warmup_enabled"] = _parse_bool_env(cache_warmup)
    cache_warmup_ttl = os.environ.get("OPENHARNESS_CACHE_WARMUP_TTL_SECONDS")
    if cache_warmup_ttl:
        updates["cache_warmup_ttl_seconds"] = float(cache_warmup_ttl)
    cache_warmup_wait = os.environ.get("OPENHARNESS_CACHE_WARMUP_WAIT_SECONDS")
    if cache_warmup_wait:
        updates["cache_warmup_wait_seconds"] = float(cache_warmup_wait)
    cache_refresh = os.environ.get("OPENHARNESS_CACHE_REFRESH")
    if cache_refresh is not None and cache_refresh.strip():
        updates["cache_refresh_enabled"] = _parse_bool_env(cache_refresh)
    cache_refresh_idle = os.environ.get("OPENHARNESS_CACHE_REFRESH_IDLE_SECONDS")
    if cache_refresh_idle:
        updates["cache_refresh_idle_seconds"] = float(cache_refresh_idle)
    offload_large_pastes = os.environ.get("OPENHARNESS_OFFLOAD_LARGE_PASTES")
    if offload_large_pastes is not None and offload_large_pastes.strip():
        updates["offload_large_pastes"] = _parse_bool_env(offload_large_pastes)
    large_paste_threshold = os.environ.get("OPENHARNESS_LARGE_PASTE_THRESHOLD_CHARS")
    if large_paste_threshold:
        updates["large_paste_threshold_chars"] = int(large_paste_threshold)
    large_paste_preview = os.environ.get("OPENHARNESS_LARGE_PASTE_PREVIEW_CHARS")
    if large_paste_preview:
        updates["large_paste_preview_chars"] = int(large_paste_preview)

    provider = os.environ.get("OPENHARNESS_PROVIDER")
    api_format = os.environ.get("OPENHARNESS_API_FORMAT")
    env_auth_source = active_profile.auth_source
    if provider or api_format:
        env_auth_source = default_auth_source_for_provider(
            provider or active_profile.provider,
            api_format or active_profile.api_format,
        )

    env_resolved = resolve_auth_env_value(env_auth_source)
    if env_resolved:
        _, api_key = env_resolved
        updates["api_key"] = api_key

    if api_format:
        updates["api_format"] = api_format

    if provider:
        updates["provider"] = provider

    sandbox_enabled = os.environ.get("OPENHARNESS_SANDBOX_ENABLED")
    sandbox_fail = os.environ.get("OPENHARNESS_SANDBOX_FAIL_IF_UNAVAILABLE")
    sandbox_backend = os.environ.get("OPENHARNESS_SANDBOX_BACKEND")
    sandbox_docker_image = os.environ.get("OPENHARNESS_SANDBOX_DOCKER_IMAGE")
    sandbox_updates: dict[str, Any] = {}
    if sandbox_enabled is not None:
        sandbox_updates["enabled"] = _parse_bool_env(sandbox_enabled)
    if sandbox_fail is not None:
        sandbox_updates["fail_if_unavailable"] = _parse_bool_env(sandbox_fail)
    if sandbox_backend is not None:
        sandbox_updates["backend"] = sandbox_backend
    if sandbox_docker_image is not None:
        sandbox_updates["docker"] = settings.sandbox.docker.model_copy(
            update={"image": sandbox_docker_image}
        )
    if sandbox_updates:
        updates["sandbox"] = settings.sandbox.model_copy(update=sandbox_updates)

    web_updates: dict[str, Any] = {}
    web_proxy = os.environ.get("OPENHARNESS_WEB_PROXY")
    if web_proxy:
        web_updates["proxy"] = web_proxy
    web_resolution_mode = os.environ.get("OPENHARNESS_WEB_RESOLUTION_MODE")
    if web_resolution_mode:
        web_updates["resolution_mode"] = web_resolution_mode
    web_synthetic_dns_cidrs = os.environ.get("OPENHARNESS_WEB_SYNTHETIC_DNS_CIDRS")
    if web_synthetic_dns_cidrs:
        web_updates["synthetic_dns_cidrs"] = [
            entry.strip()
            for entry in web_synthetic_dns_cidrs.split(",")
            if entry.strip()
        ]
    if web_updates:
        updates["web"] = settings.web.model_copy(update=web_updates)

    if not updates:
        return settings
    return settings.model_copy(update=updates)


def _parse_bool_env(value: str) -> bool:
    """Parse a boolean environment override."""
    return value.strip().lower() in {"1", "true", "yes", "on"}
