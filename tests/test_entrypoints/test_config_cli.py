"""Tests for the top-level config CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from openharness.cli import app
from openharness.config.settings import Settings, load_settings


def test_cli_config_set_persists_nested_web_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    mode_result = runner.invoke(app, ["config", "set", "web.resolution_mode", "synthetic_dns"])
    assert mode_result.exit_code == 0
    assert "Updated web.resolution_mode" in mode_result.output

    cidrs_result = runner.invoke(
        app,
        ["config", "set", "web.synthetic_dns_cidrs", "100.64.0.0/10,203.0.113.0/24"],
    )
    assert cidrs_result.exit_code == 0
    assert "Updated web.synthetic_dns_cidrs" in cidrs_result.output

    settings = load_settings()
    assert settings.web.resolution_mode == "synthetic_dns"
    assert settings.web.synthetic_dns_cidrs == ["100.64.0.0/10", "203.0.113.0/24"]


def test_cli_config_set_coerces_optional_numeric_fields(tmp_path: Path, monkeypatch):
    """Fields declared ``int | None`` must stay ints when starting from None."""
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(app, ["config", "set", "context_window_tokens", "8192"])
    assert result.exit_code == 0
    assert "Updated context_window_tokens" in result.output

    settings = load_settings()
    assert settings.context_window_tokens == 8192
    assert isinstance(settings.context_window_tokens, int)


def test_cli_config_set_rejects_bad_values_for_optional_numeric_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(app, ["config", "set", "max_tokens", "many"])
    assert result.exit_code == 1
    assert "Invalid integer value for max_tokens" in result.output
    assert load_settings().max_tokens == Settings().max_tokens


def test_cli_config_show_keeps_token_counts_visible(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    assert runner.invoke(app, ["config", "set", "api_key", "sk-should-be-hidden"]).exit_code == 0
    assert runner.invoke(app, ["config", "set", "max_tokens", "4096"]).exit_code == 0

    shown = runner.invoke(app, ["config", "show"])
    assert shown.exit_code == 0
    assert "sk-should-be-hidden" not in shown.output
    assert '"max_tokens": 4096' in shown.output
    assert '"context_window_tokens": ' in shown.output
