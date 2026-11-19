"""The persisted permission mode must have a single mutation point.

ADR-0010 D5 / ADR-0011 I3: settings.permission.mode is owned by
PermissionSettings.set_mode; every caller goes through it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "openharness"


def test_no_direct_permission_mode_assignments():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if re.search(r"permission\.mode\s*=", line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], (
        "permission.mode must be mutated only by PermissionSettings.set_mode; "
        f"direct writers: {offenders}"
    )


def test_permission_settings_exposes_the_single_writer():
    text = (SRC / "config" / "settings_schema.py").read_text(encoding="utf-8")
    assert "def set_mode(self, mode: PermissionMode | str) -> None:" in text
    assert "self.mode = mode if isinstance(mode, PermissionMode)" in text
