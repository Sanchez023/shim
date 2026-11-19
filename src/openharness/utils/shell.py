"""Shared shell and subprocess helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from openharness.config import Settings, load_settings
from openharness.platforms import PlatformName, get_platform
from openharness.sandbox import wrap_command_for_sandbox

ShellFamily = Literal["posix", "powershell", "cmd"]

_WINDOWS_BASH_INSTALL_PATHS: tuple[tuple[str, ...], ...] = (
    ("ProgramFiles", "Git", "bin", "bash.exe"),
    ("ProgramW6432", "Git", "bin", "bash.exe"),
    ("ProgramFiles(x86)", "Git", "bin", "bash.exe"),
)

# Commands run without a TTY, so anything that asks for input can only hang.
# These defaults make git fail fast (credentials, editor, pager) instead of
# waiting for a prompt the user can never answer.
_NONINTERACTIVE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_OPTIONAL_LOCKS": "0",
    "SSH_ASKPASS_REQUIRE": "never",
}


def _shell_env(env: Mapping[str, str] | None) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(_NONINTERACTIVE_ENV)
    if env is not None:
        merged.update(env)
    return merged


def _is_wsl_bash_alias(bash_path: str) -> bool:
    """Return True when ``bash_path`` is the WSL launcher, not a real bash.

    Windows ships ``bash.exe`` in two places that are *not* MSYS shells:

    * ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe`` (the Store alias)
    * ``C:\\Windows\\System32\\bash.exe`` (the legacy WSL launcher)

    Both hand the command line to a Linux distro.  That is fine for a terminal,
    but it breaks this harness in three ways: the process cannot see a Windows
    ``cwd`` as a native path, Windows executables such as ``python.exe`` are not
    on the distro's ``PATH``, and — most importantly — the launcher re-splits
    argv, so POSIX ``shlex`` quoting no longer shields shell metacharacters.
    ``bash -lc "exit 0"`` succeeds on a machine with WSL installed, so probing
    alone cannot reject it.  We must reject it by identity.
    """
    normalized = bash_path.replace("\\", "/").lower()
    return normalized.endswith(("/windowsapps/bash.exe", "/system32/bash.exe"))


def _windows_bash_candidates() -> list[str]:
    """Return bash executables worth probing, most portable first.

    ``shutil.which`` returns only the first match on ``PATH``, which on Windows
    is usually the WSL alias.  When that happens the real MSYS/Git bash installs
    are still on disk, so look for them explicitly before giving up on bash.
    """
    candidates: list[str] = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for parts in _WINDOWS_BASH_INSTALL_PATHS:
        base = os.environ.get(parts[0])
        if not base:
            continue
        candidate = Path(base).joinpath(*parts[1:])
        if candidate.exists():
            candidates.append(str(candidate))
    msys = Path(os.environ.get("SystemDrive", "C:") + "/msys64/usr/bin/bash.exe")
    if msys.exists():
        candidates.append(str(msys))

    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _usable_windows_bash() -> str | None:
    for candidate in _windows_bash_candidates():
        if _is_wsl_bash_alias(candidate):
            continue
        if _cached_bash_is_usable(candidate):
            return candidate
    return None


def resolve_shell_family(platform_name: PlatformName | None = None) -> ShellFamily:
    """Return which quoting dialect the selected shell will use.

    Hook argument substitution must speak the same dialect as the interpreter
    that ends up running the command.  Resolving both from one place keeps them
    from drifting apart.
    """
    resolved_platform = platform_name or get_platform()
    if resolved_platform != "windows":
        return "posix"
    if _usable_windows_bash() is not None:
        return "posix"
    if shutil.which("pwsh") or shutil.which("powershell"):
        return "powershell"
    return "cmd"


def _bash_login_shell_enabled(platform_name: PlatformName | None = None) -> bool:
    """Return whether bash should run as a login shell.

    On Windows a login shell sources ``/etc/profile`` on every invocation,
    which costs roughly 250 ms per command (cygpath and PATH rewriting). The
    inherited PATH already contains Git's bin directories, so the non-login
    path only re-adds the MSYS defaults. Set ``OPENHARNESS_BASH_LOGIN_SHELL=1``
    to restore the old behaviour when a profile is required.
    """
    raw = os.environ.get("OPENHARNESS_BASH_LOGIN_SHELL", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return (platform_name or get_platform()) != "windows"


def resolve_shell_command(
    command: str,
    *,
    platform_name: PlatformName | None = None,
    prefer_pty: bool = False,
) -> list[str]:
    """Return argv for the best available shell on the current platform."""
    resolved_platform = platform_name or get_platform()
    if resolved_platform == "windows":
        bash = _usable_windows_bash()
        if bash:
            if _bash_login_shell_enabled(resolved_platform):
                return [bash, "-lc", command]
            return [
                bash,
                "-c",
                f'export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"; {command}',
            ]
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            return [powershell, "-NoLogo", "-NoProfile", "-Command", command]
        return [shutil.which("cmd.exe") or "cmd.exe", "/d", "/s", "/c", command]

    bash = shutil.which("bash")
    if bash:
        argv = [bash, "-lc", command]
        if prefer_pty:
            wrapped = _wrap_command_with_script(argv, platform_name=resolved_platform)
            if wrapped is not None:
                return wrapped
        return argv
    shell = shutil.which("sh") or os.environ.get("SHELL") or "/bin/sh"
    argv = [shell, "-lc", command]
    if prefer_pty:
        wrapped = _wrap_command_with_script(argv, platform_name=resolved_platform)
        if wrapped is not None:
            return wrapped
    return argv


async def create_shell_subprocess(
    command: str,
    *,
    cwd: str | Path,
    settings: Settings | None = None,
    prefer_pty: bool = False,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = None,
    stderr: int | None = None,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a shell command with platform-aware shell selection and sandboxing."""
    resolved_settings = settings or load_settings()

    # Docker backend: route through docker exec
    if resolved_settings.sandbox.enabled and resolved_settings.sandbox.backend == "docker":
        from openharness.sandbox.session import get_docker_sandbox

        session = get_docker_sandbox()
        if session is not None and session.is_running:
            argv = resolve_shell_command(command)
            return await session.exec_command(
                argv,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=_shell_env(env),
            )
        if resolved_settings.sandbox.refuses_unavailable:
            from openharness.sandbox import SandboxUnavailableError

            raise SandboxUnavailableError("Docker sandbox session is not running")

    # Existing srt path
    argv = resolve_shell_command(command, prefer_pty=prefer_pty)
    argv, cleanup_path = wrap_command_for_sandbox(argv, settings=resolved_settings)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(Path(cwd).resolve()),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=_shell_env(env),
        )
    except Exception:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)
        raise

    if cleanup_path is not None:
        asyncio.create_task(_cleanup_after_exit(process, cleanup_path))
    return process


def _wrap_command_with_script(
    argv: list[str],
    *,
    platform_name: PlatformName | None = None,
) -> list[str] | None:
    resolved_platform = platform_name or get_platform()
    if resolved_platform == "macos":
        return None
    script = shutil.which("script")
    if script is None:
        return None
    if len(argv) >= 3 and argv[1] == "-lc":
        return [script, "-qefc", argv[2], "/dev/null"]
    return None


@lru_cache(maxsize=8)
def _cached_bash_is_usable(bash_path: str) -> bool:
    """Probe a discovered bash once per process.

    On Windows the probe spawns ``bash -c "exit 0"``, which costs a few hundred
    milliseconds; resolving the shell for every command must not pay that
    repeatedly.
    """
    return _bash_is_usable(bash_path)


def _bash_is_usable(bash_path: str) -> bool:
    """Return True when a discovered bash executable can run commands.

    On Windows, ``shutil.which("bash")`` can find WSL's ``bash.exe`` even when no
    WSL distribution is installed. In that case the executable exists but every
    command fails, so fall back to PowerShell/cmd instead of selecting it.

    The probe uses ``-c`` rather than ``-lc`` because it only needs to run a
    builtin: a login shell would source ``/etc/profile`` and every user profile
    script, which can be slow or even block.  Streams are sent to ``DEVNULL``
    rather than captured because :func:`subprocess.run` finishes a timed-out
    call with an unbounded ``communicate()`` on Windows; if a profile spawned a
    grandchild that inherited the capture pipe, that call never returns and the
    whole event loop freezes.
    """
    try:
        result = subprocess.run(
            [bash_path, "-c", "exit 0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


async def _cleanup_after_exit(process: asyncio.subprocess.Process, cleanup_path: Path) -> None:
    try:
        await process.wait()
    finally:
        cleanup_path.unlink(missing_ok=True)
