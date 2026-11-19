"""Settings file IO: load, save, lock and atomic write."""

from __future__ import annotations

import json
import os
from pathlib import Path

from openharness.config.overrides import _apply_env_overrides
from openharness.config.providers import _profile_from_flat_settings
from openharness.config.settings_schema import Settings
from openharness.utils.file_lock import exclusive_file_lock
from openharness.utils.fs import atomic_write_text


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from config file, merging with defaults.

    Args:
        config_path: Path to settings.json. If None, uses the default location.

    Returns:
        Settings instance with file values merged over defaults.
    """
    if config_path is None:
        from openharness.config.paths import get_config_file_path

        config_path = get_config_file_path()

    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        settings = Settings.model_validate(raw)
        env_profile = os.environ.get("OPENHARNESS_PROFILE")
        if env_profile:
            settings = settings.model_copy(update={"active_profile": env_profile.strip()})
        if "profiles" not in raw or "active_profile" not in raw:
            profile_name, profile = _profile_from_flat_settings(settings)
            merged_profiles = settings.merged_profiles()
            merged_profiles[profile_name] = profile
            settings = settings.model_copy(
                update={
                    "active_profile": profile_name,
                    "profiles": merged_profiles,
                }
            )
        return _apply_env_overrides(settings.materialize_active_profile())

    settings = Settings()
    env_profile = os.environ.get("OPENHARNESS_PROFILE")
    if env_profile:
        settings = settings.model_copy(update={"active_profile": env_profile.strip()})
    return _apply_env_overrides(settings.materialize_active_profile())


def save_settings(settings: Settings, config_path: Path | None = None) -> None:
    """Persist settings to the config file.

    Args:
        settings: Settings instance to save.
        config_path: Path to write. If None, uses the default location.
    """
    if config_path is None:
        from openharness.config.paths import get_config_file_path

        config_path = get_config_file_path()

    settings = settings.sync_active_profile_from_flat_fields().materialize_active_profile()
    lock_path = config_path.with_suffix(config_path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        atomic_write_text(
            config_path,
            settings.model_dump_json(indent=2) + "\n",
            # settings.json can hold a plaintext api_key (flat field); keep it
            # owner-only, matching credentials.json.
            mode=0o600,
        )
