"""Per-plugin variables and token storage.

Plugins declare variables in ``plugin.json``::

    "variables": {
      "TAVILY_API_KEY": {
        "description": "Tavily API key",
        "secret": true,
        "required": true,
        "env": "TAVILY_API_KEY"
      }
    }

Values are read from the process environment first (so CI and shell-level
overrides keep working), then from OpenHarness storage (system keyring when
available, otherwise the mode-600 credentials file).

Plugin tool modules call :func:`get_plugin_variable` instead of reading
``os.environ`` directly, so users can configure tokens with
``oh plugin config set <plugin> <KEY>``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from openharness.auth.storage import clear_credential, load_credential, store_credential

_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def plugin_credential_provider(plugin_name: str) -> str:
    """Return the credential-store provider key for a plugin."""
    return f"plugin:{plugin_name}"


def _validate(plugin_name: str, variable: str) -> None:
    if not _PLUGIN_NAME_RE.fullmatch(plugin_name or ""):
        raise ValueError(f"invalid plugin name: {plugin_name!r}")
    if not _VARIABLE_NAME_RE.fullmatch(variable or ""):
        raise ValueError(f"invalid plugin variable name: {variable!r}")


def _env_names(variable: str, env_names: Iterable[str] | None) -> tuple[str, ...]:
    candidates = tuple(env_names or ())
    return candidates or (variable,)


def plugin_variable_source(
    plugin_name: str,
    variable: str,
    *,
    env_names: Iterable[str] | None = None,
) -> str | None:
    """Return where a variable is configured: ``env:<NAME>``, ``stored``, or None."""
    _validate(plugin_name, variable)
    for env_name in _env_names(variable, env_names):
        if os.environ.get(env_name, "").strip():
            return f"env:{env_name}"
    stored = load_credential(plugin_credential_provider(plugin_name), variable)
    if stored and stored.strip():
        return "stored"
    return None


def get_plugin_variable(
    plugin_name: str,
    variable: str,
    *,
    env_names: Iterable[str] | None = None,
    default: str | None = None,
) -> str | None:
    """Resolve a plugin variable: environment, then OpenHarness storage, then default."""
    _validate(plugin_name, variable)
    for env_name in _env_names(variable, env_names):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    stored = load_credential(plugin_credential_provider(plugin_name), variable)
    if stored and stored.strip():
        return stored
    return default


def set_plugin_variable(plugin_name: str, variable: str, value: str) -> None:
    """Persist a plugin variable in the credential store."""
    _validate(plugin_name, variable)
    if not value.strip():
        raise ValueError("plugin variable value must not be empty")
    store_credential(plugin_credential_provider(plugin_name), variable, value)


def clear_plugin_variable(plugin_name: str, variable: str) -> bool:
    """Remove a stored plugin variable. Returns True when something was removed."""
    _validate(plugin_name, variable)
    return clear_credential(plugin_credential_provider(plugin_name), variable)
