#!/usr/bin/env python3
"""Build the standalone shim/ohmo bundle.

Steps:
1. Install frontend dependencies and produce ``frontend/terminal/dist/index.js``.
2. Run PyInstaller with ``packaging/shim.spec``.
3. Verify that only runtime files made it into ``dist/shim``.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "terminal"
BUNDLE = ROOT / "dist" / "shim"
FORBIDDEN_NAMES = ("TODO.md", "RELEASE_NOTES_", ".coverage")
FORBIDDEN_PARTS = ("openharness/_frontend/src", "openharness/_frontend/tsconfig.json")


def _run(command: list[str], cwd: Path) -> None:
    print(f"+ {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


def build_frontend() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is required to build the frontend; install Node.js >= 18")
    _run([npm, "ci", "--no-fund", "--no-audit"], FRONTEND)
    _run([npm, "run", "build"], FRONTEND)
    entry = FRONTEND / "dist" / "index.js"
    if not entry.exists():
        raise SystemExit(f"frontend build did not produce {entry}")
    print(f"frontend bundle: {entry} ({entry.stat().st_size / 1024 / 1024:.1f} MiB)")


def build_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("PyInstaller is missing; run `uv sync --extra build` first")
    pyinstaller = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean"]
    _run([*pyinstaller, str(ROOT / "packaging" / "shim.spec")], ROOT)
    create_ohmo_launcher()


def create_ohmo_launcher() -> None:
    """Expose the single executable as `ohmo` without duplicating the payload."""
    exe = BUNDLE / ("shim.exe" if sys.platform == "win32" else "shim")
    if sys.platform == "win32":
        launcher = BUNDLE / "ohmo.cmd"
        launcher.write_text(
            '@echo off\r\n"%~dp0shim.exe" --ohmo %*\r\n',
            encoding="utf-8",
        )
        return
    launcher = BUNDLE / "ohmo"
    if launcher.exists() or launcher.is_symlink():
        launcher.unlink()
    launcher.symlink_to(exe.name)


def verify_bundle() -> None:
    if not BUNDLE.exists():
        raise SystemExit(f"bundle not found: {BUNDLE}")
    shim_name = "shim.exe" if sys.platform == "win32" else "shim"
    ohmo_name = "ohmo.cmd" if sys.platform == "win32" else "ohmo"
    for name in (shim_name, ohmo_name):
        if not (BUNDLE / name).exists():
            raise SystemExit(f"missing executable: {BUNDLE / name}")

    offenders: list[str] = []
    for path in BUNDLE.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(BUNDLE).as_posix()
        if path.name in FORBIDDEN_NAMES or any(part in relative for part in FORBIDDEN_PARTS):
            offenders.append(relative)
    if offenders:
        raise SystemExit("unexpected files in bundle:\n" + "\n".join(offenders[:20]))

    total = sum(path.stat().st_size for path in BUNDLE.rglob("*") if path.is_file())
    print(f"bundle: {BUNDLE} ({total / 1024 / 1024:.1f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-frontend", action="store_true", help="reuse the existing dist/")
    args = parser.parse_args()

    if args.skip_frontend:
        if not (FRONTEND / "dist" / "index.js").exists():
            raise SystemExit("no prebuilt frontend; run without --skip-frontend first")
    else:
        build_frontend()
    build_pyinstaller()
    verify_bundle()


if __name__ == "__main__":
    main()
