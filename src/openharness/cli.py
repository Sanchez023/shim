"""CLI entry point using typer.

Command implementations live under :mod:`openharness.cli_commands`; importing
them registers their Typer commands on the shared ``app``. This module keeps the
public ``openharness.cli.app`` entry point and re-exports the legacy helpers.
"""

from __future__ import annotations

from openharness.cli_app import __version__, app
from openharness.cli_auth_helpers import _login_provider
from openharness.cli_commands import auth as _auth  # noqa: F401
from openharness.cli_commands import autopilot as _autopilot  # noqa: F401
from openharness.cli_commands import config as _config  # noqa: F401
from openharness.cli_commands import cron as _cron  # noqa: F401
from openharness.cli_commands import mcp as _mcp  # noqa: F401
from openharness.cli_commands import plugin as _plugin  # noqa: F401
from openharness.cli_commands import provider as _provider  # noqa: F401
from openharness.cli_commands import setup as _setup  # noqa: F401
from openharness.cli_commands.main import _version_callback, main
from openharness.cli_commands.setup import (
    _prompt_model_for_profile,
    _select_setup_workflow,
    setup_cmd,
)
from openharness.cli_preview import (
    _build_dry_run_preview,
    _format_dry_run_preview,
)
from openharness.cli_prompts import _confirm_prompt, _select_from_menu, _text_prompt
from openharness.cli_utils import _safe_short

__all__ = [
    "__version__",
    "_build_dry_run_preview",
    "_confirm_prompt",
    "_format_dry_run_preview",
    "_login_provider",
    "_prompt_model_for_profile",
    "_safe_short",
    "_select_from_menu",
    "_select_setup_workflow",
    "_text_prompt",
    "_version_callback",
    "app",
    "main",
    "setup_cmd",
]
