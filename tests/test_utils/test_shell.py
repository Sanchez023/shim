"""Tests for shell resolution helpers."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from openharness.config.settings import Settings
from openharness.utils.shell import (
    _bash_is_usable,
    _cached_bash_is_usable,
    _is_wsl_bash_alias,
    create_shell_subprocess,
    resolve_shell_command,
    resolve_shell_family,
)


def test_resolve_shell_command_prefers_bash_on_linux(monkeypatch):
    monkeypatch.setattr(
        "openharness.utils.shell.shutil.which",
        lambda name: "/usr/bin/bash" if name == "bash" else None,
    )

    command = resolve_shell_command("echo hi", platform_name="linux")

    assert command == ["/usr/bin/bash", "-lc", "echo hi"]


def test_resolve_shell_command_wraps_with_script_when_pty_requested(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "/usr/bin/bash",
            "script": "/usr/bin/script",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)

    command = resolve_shell_command("echo hi", platform_name="linux", prefer_pty=True)

    assert command == ["/usr/bin/script", "-qefc", "echo hi", "/dev/null"]


def test_resolve_shell_command_uses_powershell_on_windows(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "pwsh": "C:/Program Files/PowerShell/7/pwsh.exe",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)
    monkeypatch.setattr("openharness.utils.shell._bash_is_usable", lambda _: False)
    _cached_bash_is_usable.cache_clear()

    command = resolve_shell_command("Write-Output hi", platform_name="windows")

    assert command == [
        "C:/Program Files/PowerShell/7/pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        "Write-Output hi",
    ]


def test_resolve_shell_command_skips_script_on_macos(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "/bin/bash",
            "script": "/usr/bin/script",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)

    command = resolve_shell_command("echo hi", platform_name="macos", prefer_pty=True)

    assert command == ["/bin/bash", "-lc", "echo hi"]


def test_resolve_shell_command_linux_without_script_falls_back(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "/usr/bin/bash",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)

    command = resolve_shell_command("echo hi", platform_name="linux", prefer_pty=True)

    assert command == ["/usr/bin/bash", "-lc", "echo hi"]


def test_resolve_shell_command_windows_skips_unusable_bash(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "C:/Windows/System32/bash.exe",
            "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)
    monkeypatch.setattr("openharness.utils.shell._bash_is_usable", lambda _: False)
    _cached_bash_is_usable.cache_clear()

    command = resolve_shell_command("Write-Output hi", platform_name="windows")

    assert command == [
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        "Write-Output hi",
    ]


def test_resolve_shell_command_windows_uses_usable_bash(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "C:/Program Files/Git/bin/bash.exe",
            "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)
    monkeypatch.setattr("openharness.utils.shell._bash_is_usable", lambda _: True)

    command = resolve_shell_command("echo hi", platform_name="windows")

    assert command == [
        "C:/Program Files/Git/bin/bash.exe",
        "-c",
        'export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"; echo hi',
    ]


def test_bash_availability_probe_is_cached(monkeypatch):
    calls: list[str] = []

    def fake_probe(path: str) -> bool:
        calls.append(path)
        return True

    monkeypatch.setattr("openharness.utils.shell._bash_is_usable", fake_probe)
    _cached_bash_is_usable.cache_clear()

    assert _cached_bash_is_usable("bash") is True
    assert _cached_bash_is_usable("bash") is True
    assert calls == ["bash"]

    _cached_bash_is_usable.cache_clear()


def test_bash_is_usable_returns_true_for_zero_exit(monkeypatch):
    class _Result:
        returncode = 0

    monkeypatch.setattr("openharness.utils.shell.subprocess.run", lambda *args, **kwargs: _Result())

    assert _bash_is_usable("bash") is True


def test_bash_is_usable_probe_uses_builtin_and_devnull_streams(monkeypatch):
    """The probe must not depend on login profiles or drained pipes.

    On Windows, ``subprocess.run(timeout=...)`` finishes a timed-out call with
    an unbounded ``communicate()``; captured pipes inherited by a profile-
    spawned grandchild then keep the event loop blocked forever.
    """
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr("openharness.utils.shell.subprocess.run", fake_run)

    assert _bash_is_usable("bash") is True
    assert captured["args"] == (["bash", "-c", "exit 0"],)
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in kwargs


def test_bash_is_usable_returns_false_for_nonzero_exit(monkeypatch):
    class _Result:
        returncode = 1

    monkeypatch.setattr("openharness.utils.shell.subprocess.run", lambda *args, **kwargs: _Result())

    assert _bash_is_usable("bash") is False


def test_bash_is_usable_returns_false_for_spawn_errors(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="bash", timeout=5)

    monkeypatch.setattr("openharness.utils.shell.subprocess.run", raise_timeout)

    assert _bash_is_usable("bash") is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="this test asserts the POSIX bash argv shape",
)
async def test_create_shell_subprocess_defaults_stdin_to_devnull(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class _FakeProcess:
            returncode = 0

            async def wait(self):
                return 0

        return _FakeProcess()

    monkeypatch.setattr(
        "openharness.utils.shell.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        "openharness.utils.shell.wrap_command_for_sandbox",
        lambda argv, settings=None: (argv, None),
    )
    monkeypatch.setattr(
        "openharness.utils.shell.shutil.which",
        lambda name: "/usr/bin/bash" if name == "bash" else None,
    )

    await create_shell_subprocess(
        "echo hi",
        cwd=tmp_path,
        settings=Settings(),
    )

    assert captured["args"] == ("/usr/bin/bash", "-lc", "echo hi")
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL

def test_wsl_bash_alias_is_recognized_by_identity():
    """The WSL launcher is rejected by identity, not by probing.

    ``bash -lc "exit 0"`` succeeds on any machine with WSL installed, so a
    liveness probe cannot distinguish it from a real MSYS shell.
    """
    assert _is_wsl_bash_alias(
        r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe"
    )
    assert _is_wsl_bash_alias(r"C:\Windows\System32\bash.exe")
    assert not _is_wsl_bash_alias(r"C:\Program Files\Git\bin\bash.exe")


def test_resolve_shell_command_skips_wsl_alias_even_when_probe_succeeds(monkeypatch):
    """A usable-looking WSL launcher must not win over PowerShell."""
    alias = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe"
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    monkeypatch.setattr(
        "openharness.utils.shell._windows_bash_candidates", lambda: [alias]
    )
    monkeypatch.setattr("openharness.utils.shell._cached_bash_is_usable", lambda _: True)
    monkeypatch.setattr(
        "openharness.utils.shell.shutil.which",
        lambda name: powershell if name == "powershell" else None,
    )

    command = resolve_shell_command("Write-Output hi", platform_name="windows")

    assert command == [powershell, "-NoLogo", "-NoProfile", "-Command", "Write-Output hi"]

def test_shell_family_matches_the_shell_that_will_run_the_command(monkeypatch):
    """Hook quoting is chosen from the same decision as shell selection."""
    monkeypatch.setattr("openharness.utils.shell._windows_bash_candidates", list)
    monkeypatch.setattr(
        "openharness.utils.shell.shutil.which",
        lambda name: r"C:\Program Files\PowerShell\7\pwsh.exe" if name == "pwsh" else None,
    )

    assert resolve_shell_family("windows") == "powershell"
    assert resolve_shell_family("linux") == "posix"
    assert resolve_shell_family("macos") == "posix"


def test_windows_bash_login_shell_can_be_restored(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {
            "bash": "C:/Program Files/Git/bin/bash.exe",
            "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.utils.shell.shutil.which", fake_which)
    monkeypatch.setattr("openharness.utils.shell._bash_is_usable", lambda _: True)
    monkeypatch.setenv("OPENHARNESS_BASH_LOGIN_SHELL", "1")

    command = resolve_shell_command("echo hi", platform_name="windows")

    assert command == ["C:/Program Files/Git/bin/bash.exe", "-lc", "echo hi"]


def test_shell_env_sets_git_noninteractive_defaults(monkeypatch):
    from openharness.utils.shell import _shell_env

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    env = _shell_env(None)

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_PAGER"] == "cat"
    assert env["PAGER"] == "cat"
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_SEQUENCE_EDITOR"] == "true"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["SSH_ASKPASS_REQUIRE"] == "never"


def test_shell_env_caller_values_win(monkeypatch):
    from openharness.utils.shell import _shell_env

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    env = _shell_env({"GIT_TERMINAL_PROMPT": "1"})

    assert env["GIT_TERMINAL_PROMPT"] == "1"
