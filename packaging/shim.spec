# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone shim/ohmo bundle.

The bundle contains the Python runtime, application code, bundled skills, and
the prebuilt React terminal entry point.  It deliberately does not include
tests, docs, benchmarks, assets, or repository-only tooling.
"""

from __future__ import annotations

import glob
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

# Runtime data that is read from the package at import/run time.
datas = [
    (str(ROOT / "frontend" / "terminal" / "package.json"), "openharness/_frontend"),
    (str(ROOT / "frontend" / "terminal" / "dist" / "index.js"), "openharness/_frontend/dist"),
    (str(ROOT / "src" / "openharness" / "sandbox" / "Dockerfile"), "openharness/sandbox"),
]
for skill_path in glob.glob(str(ROOT / "src" / "openharness" / "skills" / "bundled" / "content" / "*.md")):
    datas.append((skill_path, "openharness/skills/bundled/content"))

# Modules resolved through importlib/getattr rather than static imports.
hiddenimports = [
    "openharness.runtime.registry",
    "openharness.runtime.scope",
    "openharness.swarm.mailbox",
    "openharness.swarm.permission_sync",
    "openharness.swarm.in_process",
    "openharness.swarm.subprocess_backend",
    "openharness.channels.impl.dingtalk",
    "openharness.channels.impl.discord",
    "openharness.channels.impl.email",
    "openharness.channels.impl.feishu",
    "openharness.channels.impl.matrix",
    "openharness.channels.impl.mochat",
    "openharness.channels.impl.qq",
    "openharness.channels.impl.slack",
    "openharness.channels.impl.telegram",
    "openharness.channels.impl.whatsapp",
]

# Development-only modules.  PyInstaller only follows imports, so this mainly
# guards against accidental collection through optional dependency hooks.
excludes = [
    "tests",
    "pytest",
    "pytest_asyncio",
    "mypy",
    "ruff",
    "pexpect",
    "human_eval",
    "benchmarks",
    "scripts",
    "numpy",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src"), str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

shim = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="shim",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    shim,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="shim",
)
