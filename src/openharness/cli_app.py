"""Shared Typer application and sub-app instances."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import typer

from openharness.version import __version__

# ``cli`` and ``cli_commands.main`` re-export the version from this module, so
# declare the re-export explicitly for type checkers.
__all__ = ["__version__"]

_RESUME_OPTIONS = frozenset({"--resume", "-r"})


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """Give a bare ``--resume`` / ``-r`` an empty value.

    Click cannot model an option whose value is optional, but ``shim --resume``
    (no session id) is the documented way to open the interactive session
    picker. Rewriting it to ``--resume ""`` keeps every other flag untouched:
    ``--resume abc`` and ``--resume=abc`` pass through unchanged.
    """
    normalized: list[str] = []
    index = 0
    total = len(argv)
    while index < total:
        argument = argv[index]
        normalized.append(argument)
        index += 1
        if argument in _RESUME_OPTIONS and (
            index >= total or str(argv[index]).startswith("-")
        ):
            normalized.append("")
    return normalized


class _ResumeNormalizingGroup(typer.core.TyperGroup):
    """Click group that accepts ``--resume`` without a session id.

    Typer turns its ``Typer`` instance into a Click command via
    ``typer.main.get_command``, so the hook has to live on the group class
    (``typer.Typer(cls=...)``) rather than on ``Typer.main``.
    """

    def main(self, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], (list, tuple)):
            args = (normalize_argv(list(args[0])), *args[1:])
        elif isinstance(kwargs.get("args"), (list, tuple)):
            kwargs = {**kwargs, "args": normalize_argv(list(kwargs["args"]))}
        elif not (args and isinstance(args[0], str)):
            # Launched from a console script: sys.argv is the only source.
            sys.argv[1:] = normalize_argv(sys.argv[1:])
        return super().main(*args, **kwargs)


app = typer.Typer(
    name="shim",
    help=(
        "Shim — Oh my Harness! An AI-powered coding assistant.\n\n"
        "Starts an interactive session by default, use -p/--print for non-interactive output."
    ),
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
    cls=_ResumeNormalizingGroup,
)

mcp_app = typer.Typer(name="mcp", help="Manage MCP servers")

plugin_app = typer.Typer(name="plugin", help="Manage plugins")

auth_app = typer.Typer(name="auth", help="Manage authentication")

provider_app = typer.Typer(name="provider", help="Manage provider profiles")

config_app = typer.Typer(name="config", help="Show or update settings")

cron_app = typer.Typer(name="cron", help="Manage cron scheduler and jobs")

autopilot_app = typer.Typer(name="autopilot", help="Manage repo autopilot")

app.add_typer(mcp_app)

app.add_typer(plugin_app)

app.add_typer(auth_app)

app.add_typer(provider_app)

app.add_typer(config_app)

app.add_typer(cron_app)

app.add_typer(autopilot_app)
