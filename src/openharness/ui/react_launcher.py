"""Launch the default React terminal frontend."""

from __future__ import annotations

import sys
from pathlib import Path

from openharness.ui.frontend_runtime import get_frontend_dir, launch_frontend


def _resolve_version() -> str:
    """Return the package version shown in the TUI banner."""
    try:
        from openharness.version import __version__

        return __version__
    except Exception:
        return ""


def _resolve_theme() -> str:
    """Read the theme name from settings, defaulting to 'default'."""
    try:
        from openharness.config.settings import load_settings
        return load_settings().theme or "default"
    except Exception:
        return "default"


def build_backend_command(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
    resume_session_id: str | None = None,
    continue_session: bool = False,
) -> list[str]:
    """Return the command used by the React frontend to spawn the backend host.

    ``resume_session_id`` / ``continue_session`` are forwarded as CLI flags so the
    backend child loads the snapshot itself; otherwise ``--continue``/``--resume``
    would restore nothing (the frontend only spawns the backend command).
    """
    command = [sys.executable, "-m", "openharness", "--backend-only"]
    if cwd:
        command.extend(["--cwd", cwd])
    if model:
        command.extend(["--model", model])
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])
    if effort:
        command.extend(["--effort", effort])
    if personality:
        command.extend(["--personality", personality])
    if tool_profile:
        command.extend(["--tool-profile", tool_profile])
    if base_url:
        command.extend(["--base-url", base_url])
    if system_prompt:
        command.extend(["--system-prompt", system_prompt])
    if api_key:
        command.extend(["--api-key", api_key])
    if api_format:
        command.extend(["--api-format", api_format])
    if permission_mode:
        command.extend(["--permission-mode", permission_mode])
    if resume_session_id:
        command.extend(["--resume", resume_session_id])
    elif continue_session:
        command.append("--continue")
    return command


async def launch_react_tui(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    personality: str | None = None,
    tool_profile: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
    resume_session_id: str | None = None,
    continue_session: bool = False,
    open_session_picker: bool = False,
) -> int:
    """Launch the React terminal frontend as the default UI."""
    config = {
        "backend_command": build_backend_command(
            cwd=cwd or str(Path.cwd()),
            model=model,
            max_turns=max_turns,
            effort=effort,
            personality=personality,
            tool_profile=tool_profile,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            permission_mode=permission_mode,
            resume_session_id=resume_session_id,
            continue_session=continue_session,
        ),
        "initial_prompt": prompt,
        "initial_modal": "resume" if open_session_picker else None,
        "theme": _resolve_theme(),
        "app_name": "shim",
        "version": _resolve_version(),
    }
    return await launch_frontend(get_frontend_dir(), config)


__all__ = ["build_backend_command", "get_frontend_dir", "launch_react_tui"]
