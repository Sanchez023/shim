"""Compatibility re-exports for the settings schema and IO.

The implementation is split by responsibility (ADR-0010 D6):

- :mod:`openharness.config.settings_schema` — Pydantic models and ``Settings``;
- :mod:`openharness.config.providers` — provider catalog and resolution;
- :mod:`openharness.config.overrides` — environment-variable overrides;
- :mod:`openharness.config.io` — file loading/saving and atomic writes.
"""

from __future__ import annotations

from openharness.config.io import load_settings, save_settings
from openharness.config.overrides import _apply_env_overrides, _parse_bool_env
from openharness.config.provider_types import ProviderProfile, ResolvedAuth
from openharness.config.providers import (
    CLAUDE_MODEL_ALIAS_OPTIONS,
    auth_source_env_var_candidates,
    auth_source_provider_name,
    auth_source_uses_api_key,
    builtin_provider_profile_names,
    credential_storage_provider_name,
    default_auth_source_for_provider,
    default_provider_profiles,
    display_label_for_profile,
    display_model_setting,
    is_claude_family_provider,
    normalize_anthropic_model_name,
    resolve_auth_env_value,
    resolve_model_setting,
)
from openharness.config.settings_schema import (
    DockerSandboxSettings,
    ImageGenerationConfig,
    MemorySettings,
    NotificationSettings,
    PathRuleConfig,
    PermissionSettings,
    SandboxFilesystemSettings,
    SandboxNetworkSettings,
    SandboxSettings,
    Settings,
    VerificationSettings,
    VisionModelConfig,
    WebSettings,
    strip_ansi_escape_sequences,
)

__all__ = [
    "CLAUDE_MODEL_ALIAS_OPTIONS",
    "DockerSandboxSettings",
    "ImageGenerationConfig",
    "MemorySettings",
    "NotificationSettings",
    "PathRuleConfig",
    "PermissionSettings",
    "ProviderProfile",
    "ResolvedAuth",
    "SandboxFilesystemSettings",
    "SandboxNetworkSettings",
    "SandboxSettings",
    "Settings",
    "VerificationSettings",
    "VisionModelConfig",
    "WebSettings",
    "_apply_env_overrides",
    "_parse_bool_env",
    "auth_source_env_var_candidates",
    "auth_source_provider_name",
    "auth_source_uses_api_key",
    "builtin_provider_profile_names",
    "credential_storage_provider_name",
    "default_auth_source_for_provider",
    "default_provider_profiles",
    "display_label_for_profile",
    "display_model_setting",
    "is_claude_family_provider",
    "load_settings",
    "normalize_anthropic_model_name",
    "resolve_auth_env_value",
    "resolve_model_setting",
    "save_settings",
    "strip_ansi_escape_sequences",
]
