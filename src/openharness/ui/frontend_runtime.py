"""Locate and launch the React terminal frontend.

A packaged build ships a pre-bundled ``dist/index.js`` and only needs a Node
runtime.  A source checkout without that bundle falls back to ``tsx`` plus an
``npm install`` on first launch, which keeps development unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

FRONTEND_CONFIG_ENV = "OPENHARNESS_FRONTEND_CONFIG"
MIN_NODE_MAJOR = 18


def _resolve_npm() -> str:
    """Resolve the npm executable (npm.cmd on Windows)."""
    return shutil.which("npm") or "npm"


def _resolve_tsx(frontend_dir: Path) -> tuple[str, ...]:
    """Resolve the tsx command to invoke directly, bypassing ``npm exec``.

    On Windows / WSL the ``npm exec -- tsx`` wrapper chain often spawns
    intermediate ``cmd.exe`` / shell processes that break TTY stdin
    inheritance.  Calling the ``tsx`` binary directly preserves the TTY so
    that Ink's ``useInput`` (which requires raw-mode stdin) keeps working.
    """
    bin_dir = frontend_dir / "node_modules" / ".bin"
    if sys.platform == "win32":
        for name in ("tsx.cmd", "tsx.ps1", "tsx"):
            candidate = bin_dir / name
            if candidate.exists():
                return (str(candidate),)
    else:
        candidate = bin_dir / "tsx"
        if candidate.exists():
            return (str(candidate),)

    global_tsx = shutil.which("tsx")
    if global_tsx:
        return (global_tsx,)

    return (_resolve_npm(), "exec", "--", "tsx")


def _candidate_frontend_dirs() -> list[Path]:
    """Return frontend locations in lookup order."""
    package_dir = Path(__file__).resolve().parent.parent / "_frontend"
    candidates = [package_dir]

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "openharness" / "_frontend")

    # PyInstaller onedir keeps bundled modules under ``_internal``.
    candidates.append(
        Path(sys.executable).resolve().parent / "_internal" / "openharness" / "_frontend"
    )

    # Source checkout: <repo>/frontend/terminal/
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "frontend" / "terminal")
    return candidates


def get_frontend_dir() -> Path:
    """Return the first frontend directory containing ``package.json``."""
    candidates = _candidate_frontend_dirs()
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    return candidates[0]


def find_prebuilt_entry(frontend_dir: Path) -> Path | None:
    """Return the bundled JS entry point, if it has been built."""
    for name in ("dist/index.js", "dist/index.mjs", "dist/index.cjs"):
        entry = frontend_dir / name
        if entry.exists():
            return entry
    return None


def resolve_node() -> str | None:
    """Return the Node executable path, or ``None`` when unavailable."""
    return shutil.which("node")


def require_node() -> str:
    """Return a Node >= 18 executable or raise a user-facing error."""
    node = resolve_node()
    if node is None:
        raise RuntimeError(
            "Node.js >= 18 is required to run the terminal UI. "
            "Install it from https://nodejs.org/ or run a headless command "
            "such as `shim -p \"...\"`."
        )
    try:
        completed = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = completed.stdout.strip().lstrip("v")
        major = int(version.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not determine the Node.js version: {exc}") from exc
    if major < MIN_NODE_MAJOR:
        raise RuntimeError(
            f"Node.js >= {MIN_NODE_MAJOR} is required for the terminal UI; "
            f"found v{version}. Upgrade Node.js or run a headless command."
        )
    return node


async def _ensure_dev_dependencies(frontend_dir: Path) -> None:
    """Install frontend dependencies for a source checkout."""
    if (frontend_dir / "node_modules").exists():
        return
    install = await asyncio.create_subprocess_exec(
        _resolve_npm(),
        "install",
        "--no-fund",
        "--no-audit",
        cwd=str(frontend_dir),
    )
    if await install.wait() != 0:
        raise RuntimeError("Failed to install React terminal frontend dependencies")


async def launch_frontend(frontend_dir: Path, config: dict[str, Any]) -> int:
    """Launch the frontend with *config* and return its exit code."""
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"React terminal frontend is missing: {package_json}")

    env = os.environ.copy()
    env[FRONTEND_CONFIG_ENV] = json.dumps(config)

    prebuilt = find_prebuilt_entry(frontend_dir)
    node = require_node()
    command: tuple[str, ...]
    if prebuilt is not None:
        command = (node, str(prebuilt))
    else:
        await _ensure_dev_dependencies(frontend_dir)
        command = (*_resolve_tsx(frontend_dir), "src/index.tsx")

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(frontend_dir),
        env=env,
        stdin=None,
        stdout=None,
        stderr=None,
    )
    return await process.wait()


__all__ = [
    "FRONTEND_CONFIG_ENV",
    "MIN_NODE_MAJOR",
    "find_prebuilt_entry",
    "get_frontend_dir",
    "launch_frontend",
    "require_node",
    "resolve_node",
]
