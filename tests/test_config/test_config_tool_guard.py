"""The ``config`` tool must not be a privilege-escalation or secret-leak path.

History: ``set`` did a blind ``setattr`` on any dotted path, so one approval of
``config`` could persist ``permission.mode=full_auto``; ``show`` returned
``model_dump_json()`` verbatim, including plaintext API keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openharness.tools.base import ToolExecutionContext
from openharness.tools.config_tool import ConfigTool, ConfigToolInput


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "permission.mode",
        "sandbox.enabled",
        "hooks.PostToolUse",
        "mcp_servers.evil.command",
        "api_key",
        "base_url",
        "model",
        "profiles.deepseek.api_key",
        "allow_project_plugins",
        "enabled_plugins.x",
    ],
)
async def test_set_refuses_privileged_keys(isolated_config: Path, key: str) -> None:
    result = await ConfigTool().execute(
        ConfigToolInput(action="set", key=key, value="full_auto"),
        ToolExecutionContext(cwd=isolated_config),
    )
    assert result.is_error is True
    assert "Refusing to set" in result.output

    settings_text = (isolated_config / "settings.json").read_text(encoding="utf-8") \
        if (isolated_config / "settings.json").exists() else ""
    assert "full_auto" not in settings_text


@pytest.mark.asyncio
async def test_set_allows_cosmetic_keys(isolated_config: Path) -> None:
    result = await ConfigTool().execute(
        ConfigToolInput(action="set", key="theme", value="solarized"),
        ToolExecutionContext(cwd=isolated_config),
    )
    assert result.is_error is False
    assert result.output == "Updated theme"


@pytest.mark.asyncio
async def test_show_masks_secrets(isolated_config: Path) -> None:
    (isolated_config / "settings.json").write_text(
        json.dumps(
            {
                "api_key": "sk-" + "a" * 40,
                "model": "sonnet",
                "vision": {"api_key": "sk-" + "b" * 40},
                "mcp_servers": {
                    "x": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer " + "c" * 40},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = await ConfigTool().execute(
        ConfigToolInput(action="show"),
        ToolExecutionContext(cwd=isolated_config),
    )
    assert "a" * 40 not in result.output
    assert "b" * 40 not in result.output
    assert "c" * 40 not in result.output
    assert "sonnet" in result.output  # non-secret values stay readable


def test_saved_settings_are_owner_only(tmp_path: Path) -> None:
    import os

    from openharness.config.settings import Settings, save_settings

    target = tmp_path / "settings.json"
    save_settings(Settings(api_key="sk-" + "z" * 40), target)

    if os.name == "nt":  # pragma: no cover - Windows ACLs are not POSIX bits
        pytest.skip("POSIX mode check")
    assert (target.stat().st_mode & 0o777) == 0o600
