"""Cross-platform asyncio shutdown compatibility.

On Windows, the Proactor event loop sometimes reports a benign
``ConnectionResetError`` while tearing down a transport::

    Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
    ...
    self._sock.shutdown(socket.SHUT_RDWR)
    ConnectionResetError: [WinError 10054] ...

The peer has already reset the connection, so the callback cannot do anything
useful. asyncio routes it through ``call_exception_handler``, which prints a
frightening traceback on exit. Filter exactly that teardown case and leave all
other loop errors untouched.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_FILTER_FLAG = "_openharness_reset_filter_installed"
_BENIGN_RESET_TYPES = (ConnectionResetError, ConnectionAbortedError)


def is_benign_shutdown_reset(
    context: dict[str, Any],
    *,
    platform_name: str | None = None,
) -> bool:
    """Return True for the known Windows connection-reset-during-close noise."""
    platform = platform_name or sys.platform
    if platform != "win32":
        return False
    exception = context.get("exception")
    if not isinstance(exception, _BENIGN_RESET_TYPES):
        return False
    message = str(context.get("message") or "")
    return "_call_connection_lost" in message or "connection_lost" in message


def install_loop_exception_filter(
    loop: asyncio.AbstractEventLoop,
    *,
    platform_name: str | None = None,
) -> None:
    """Install the shutdown-reset filter on one event loop (Windows only)."""
    platform = platform_name or sys.platform
    if platform != "win32":
        return
    if getattr(loop, _FILTER_FLAG, False):
        return
    previous = loop.get_exception_handler()

    def _handler(active_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_benign_shutdown_reset(context, platform_name=platform):
            logger.debug(
                "Suppressed Windows connection reset during shutdown: %s",
                context.get("exception"),
            )
            return
        if previous is not None:
            previous(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    setattr(loop, _FILTER_FLAG, True)


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with the Windows shutdown-reset filter installed."""
    async def _runner() -> T:
        install_loop_exception_filter(asyncio.get_running_loop())
        return await coro

    return asyncio.run(_runner())
