"""Tests for plugin variables and token storage."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openharness.cli import app
from openharness.plugins.config import (
    clear_plugin_variable,
    get_plugin_variable,
    plugin_variable_source,
    set_plugin_variable,
)
from openharness.plugins.installer import install_plugin_from_path

REPO_ROOT = Path(__file__).resolve().parents[2]
TAVILY_PLUGIN = REPO_ROOT / "plugins" / "tavily-search"


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("openharness.auth.storage._keyring_checked", True)
    monkeypatch.setattr("openharness.auth.storage._keyring_usable", False)
    for name in ("TAVILY_API_KEY", "TVLY_API_KEY", "OPENHARNESS_TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_plugin_variable_round_trip(isolated_store) -> None:
    assert get_plugin_variable("tavily-search", "TAVILY_API_KEY") is None

    set_plugin_variable("tavily-search", "TAVILY_API_KEY", "tvly-stored")
    assert get_plugin_variable("tavily-search", "TAVILY_API_KEY") == "tvly-stored"
    assert plugin_variable_source("tavily-search", "TAVILY_API_KEY") == "stored"

    assert clear_plugin_variable("tavily-search", "TAVILY_API_KEY") is True
    assert get_plugin_variable("tavily-search", "TAVILY_API_KEY") is None
    assert clear_plugin_variable("tavily-search", "TAVILY_API_KEY") is False


def test_environment_overrides_stored_value(isolated_store, monkeypatch) -> None:
    set_plugin_variable("tavily-search", "TAVILY_API_KEY", "tvly-stored")
    monkeypatch.setenv("TVLY_API_KEY", "tvly-from-env")

    assert (
        get_plugin_variable(
            "tavily-search",
            "TAVILY_API_KEY",
            env_names=("TAVILY_API_KEY", "TVLY_API_KEY"),
        )
        == "tvly-from-env"
    )
    assert (
        plugin_variable_source(
            "tavily-search",
            "TAVILY_API_KEY",
            env_names=("TAVILY_API_KEY", "TVLY_API_KEY"),
        )
        == "env:TVLY_API_KEY"
    )


def test_default_is_used_when_nothing_is_configured(isolated_store) -> None:
    assert (
        get_plugin_variable(
            "tavily-search",
            "UNSET_VARIABLE",
            default="fallback",
        )
        == "fallback"
    )


def test_cli_config_set_show_and_unset_masks_secrets(isolated_store) -> None:
    install_plugin_from_path(TAVILY_PLUGIN)
    runner = CliRunner()

    listed = runner.invoke(app, ["plugin", "list"])
    assert listed.exit_code == 0
    assert "missing: TAVILY_API_KEY" in listed.output

    stored = runner.invoke(
        app,
        [
            "plugin",
            "config",
            "set",
            "tavily-search",
            "TAVILY_API_KEY",
            "--value",
            "tvly-cli-secret-value",
        ],
    )
    assert stored.exit_code == 0
    assert "Stored TAVILY_API_KEY" in stored.output

    shown = runner.invoke(app, ["plugin", "config", "show", "tavily-search"])
    assert shown.exit_code == 0
    assert "TAVILY_API_KEY" in shown.output
    assert "tvly-cli-secret-value" not in shown.output
    assert "tvly…alue" in shown.output

    listed_after = runner.invoke(app, ["plugin", "list"])
    assert listed_after.exit_code == 0
    assert "missing: TAVILY_API_KEY" not in listed_after.output

    removed = runner.invoke(
        app,
        ["plugin", "config", "unset", "tavily-search", "TAVILY_API_KEY"],
    )
    assert removed.exit_code == 0
    assert "Removed TAVILY_API_KEY" in removed.output
