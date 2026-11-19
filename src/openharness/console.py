"""Console encoding setup for Windows terminals.

Windows consoles commonly default to a legacy code page (for example GBK /
cp936). Python then encodes ``print()`` output with that code page, which raises
``UnicodeEncodeError`` for characters outside it (emoji, arrows, box drawing,
some CJK extensions) and crashes the CLI mid-turn.

``configure_console_encoding`` keeps output lossless on modern UTF-8-capable
terminals and, at minimum, degrades to replacement characters instead of
crashing when stdout is redirected.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


def configure_console_encoding() -> None:
    """Make stdout/stderr tolerant of Unicode on legacy Windows code pages."""
    if os.name != "nt":
        return
    _configure_stream(sys.stdout)
    _configure_stream(sys.stderr)
    _configure_input()


def _configure_input() -> None:
    """Decode redirected stdin as UTF-8; console input already uses wide APIs."""
    stream = sys.stdin
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        if not stream.isatty():
            reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass


def _configure_stream(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        if stream.isatty():
            if _enable_utf8_console():
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")
        else:
            # Redirected output is almost always consumed as UTF-8 (files,
            # pipes, CI logs); keep it consistent with the UTF-8 file I/O used
            # elsewhere in the project.
            reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        # Detached/closed streams and test doubles may not allow
        # reconfiguration; leaving the default encoding is still safe.
        pass


def _enable_utf8_console() -> bool:
    """Switch the attached Windows console to code page 65001 (UTF-8)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        output_ok = bool(kernel32.SetConsoleOutputCP(65001))
        input_ok = bool(kernel32.SetConsoleCP(65001))
        return output_ok or input_ok
    except (AttributeError, OSError):
        return False
