"""Channel SDKs are optional dependencies.

Installing ``shim`` must not pull slack-sdk/discord/telegram just to run the
CLI, so nothing on the default import path may import them eagerly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

OPTIONAL_SDKS = ("telegram", "slack_sdk", "lark_oapi", "botpy", "discord")

_ROOT = Path(__file__).resolve().parents[2]


def _imported_modules(statement: str) -> set[str]:
    """Import *statement* in a fresh interpreter and list channel SDK modules."""
    probe = (
        "import sys\n"
        f"{statement}\n"
        f"names = {OPTIONAL_SDKS!r}\n"
        "print('|'.join(sorted(n for n in names if n in sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_ROOT / "src")},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {name for name in result.stdout.strip().split("|") if name}


@pytest.mark.parametrize(
    "statement",
    [
        "import openharness.channels",
        "import openharness.channels.impl.manager",
        "import openharness.cli_app",
    ],
)
def test_default_imports_do_not_pull_channel_sdks(statement: str) -> None:
    assert _imported_modules(statement) == set()
