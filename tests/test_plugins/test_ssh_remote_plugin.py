"""Tests for the ssh-remote plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from openharness.config.settings import Settings
from openharness.plugins import load_plugins
from openharness.tools.base import ToolExecutionContext

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins"
TOOL_PATH = PLUGIN_ROOT / "ssh-remote" / "tools" / "ssh_exec.py"


def _load_tool_module():
    module_name = "ssh_exec_under_test"
    spec = importlib.util.spec_from_file_location(module_name, TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_plugin_loads_its_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))

    plugins = load_plugins(Settings(), tmp_path, extra_roots=[PLUGIN_ROOT])

    ssh_remote = next(plugin for plugin in plugins if plugin.name == "ssh-remote")
    assert ssh_remote.enabled is True
    assert [tool.name for tool in ssh_remote.tools] == ["ssh_exec"]


def test_wsl_command_enables_control_master() -> None:
    module = _load_tool_module()

    argv = module.build_ssh_command(
        host="example.com",
        user="root",
        port=22,
        command="hostname",
        persist_seconds=600,
        use_wsl=True,
    )

    assert argv[:3] == ["wsl.exe", "-e", "ssh"]
    assert "ControlMaster=auto" in argv
    assert "ControlPath=/tmp/oh-ssh-%C" in argv
    assert "ControlPersist=600" in argv
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["root@example.com", "hostname"]


def test_native_command_uses_home_control_path() -> None:
    module = _load_tool_module()

    argv = module.build_ssh_command(
        host="example.com",
        user="deploy",
        port=2222,
        command="uptime",
        identity_file="/home/deploy/.ssh/id_ed25519",
        accept_new_host_key=False,
        use_wsl=False,
    )

    assert argv[0] == "ssh"
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and "/home/deploy/.ssh/id_ed25519" in argv
    assert "StrictHostKeyChecking=accept-new" not in argv
    assert any(arg.startswith("ControlPath=") and arg.endswith("cm-%C") for arg in argv)
    assert argv[-2:] == ["deploy@example.com", "uptime"]


def test_rejects_option_injection() -> None:
    module = _load_tool_module()

    with pytest.raises(ValueError, match="invalid host"):
        module.build_ssh_command(
            host="-oProxyCommand=calc.exe",
            command="true",
            use_wsl=False,
        )
    with pytest.raises(ValueError, match="invalid user"):
        module.build_ssh_command(
            host="example.com",
            user="root -oProxyCommand=calc.exe",
            command="true",
            use_wsl=False,
        )


@pytest.mark.asyncio
async def test_execute_formats_output(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool_module()

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"hostname-output\n", b""

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    tool = module.SshExecTool()

    result = await tool.execute(
        module.SshExecInput(host="example.com", user="root", command="hostname"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert result.output == "hostname-output"
    assert result.metadata["returncode"] == 0
    assert result.metadata["transport"] in {"wsl", "native"}
