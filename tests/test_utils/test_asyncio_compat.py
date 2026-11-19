"""Tests for the Windows asyncio shutdown-reset filter."""

from __future__ import annotations

import asyncio

import pytest

from openharness.utils.asyncio_compat import (
    install_loop_exception_filter,
    is_benign_shutdown_reset,
    run_async,
)

BENIGN_CONTEXT = {
    "message": (
        "Exception in callback "
        "_ProactorBasePipeTransport._call_connection_lost(None)"
    ),
    "exception": ConnectionResetError(10054, "connection reset"),
}


def test_benign_reset_detection_is_windows_teardown_only() -> None:
    assert is_benign_shutdown_reset(BENIGN_CONTEXT, platform_name="win32") is True
    assert is_benign_shutdown_reset(BENIGN_CONTEXT, platform_name="linux") is False
    assert (
        is_benign_shutdown_reset(
            {"message": "unrelated", "exception": ConnectionResetError()},
            platform_name="win32",
        )
        is False
    )
    assert (
        is_benign_shutdown_reset(
            {"message": BENIGN_CONTEXT["message"], "exception": ValueError("x")},
            platform_name="win32",
        )
        is False
    )


def test_filter_suppresses_teardown_reset_and_forwards_other_errors() -> None:
    loop = asyncio.new_event_loop()
    forwarded: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: forwarded.append(context))
    try:
        install_loop_exception_filter(loop, platform_name="win32")
        handler = loop.get_exception_handler()
        assert handler is not None

        loop.call_exception_handler(BENIGN_CONTEXT)
        assert forwarded == []

        other = {"message": "something else", "exception": RuntimeError("boom")}
        loop.call_exception_handler(other)
        assert forwarded == [other]
    finally:
        loop.close()


def test_filter_is_a_noop_off_windows() -> None:
    loop = asyncio.new_event_loop()
    try:
        install_loop_exception_filter(loop, platform_name="linux")
        assert loop.get_exception_handler() is None
    finally:
        loop.close()


def test_run_async_returns_values_and_propagates_errors() -> None:
    async def _value() -> int:
        return 42

    async def _boom() -> None:
        raise RuntimeError("boom")

    assert run_async(_value()) == 42
    with pytest.raises(RuntimeError, match="boom"):
        run_async(_boom())
