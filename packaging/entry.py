"""Shared PyInstaller entry point for the `shim` and `ohmo` executables."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def main() -> None:
    multiprocessing.freeze_support()
    executable = Path(sys.argv[0]).stem.lower()
    as_ohmo = executable == "ohmo"
    if sys.argv[1:2] == ["--ohmo"]:
        as_ohmo = True
        del sys.argv[1]
    if as_ohmo:
        from ohmo.cli import app
    else:
        from openharness.cli import app
    app()


if __name__ == "__main__":
    main()
