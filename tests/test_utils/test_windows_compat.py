"""Regressions for Windows console and subprocess text encoding."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openharness.commands.helpers import _run_git_command
from openharness.console import _configure_input, _configure_stream


class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty
        self.calls: list[dict[str, str]] = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_redirected_stream_is_reconfigured_to_utf8() -> None:
    stream = _FakeStream(tty=False)

    _configure_stream(stream)  # type: ignore[arg-type]

    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_redirected_stdin_is_reconfigured_to_utf8(monkeypatch) -> None:
    stream = _FakeStream(tty=False)
    monkeypatch.setattr("openharness.console.sys.stdin", stream)

    _configure_input()

    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_tty_stream_falls_back_when_utf8_console_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("openharness.console._enable_utf8_console", lambda: False)
    stream = _FakeStream(tty=True)

    _configure_stream(stream)  # type: ignore[arg-type]

    assert stream.calls == [{"errors": "replace"}]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_output_with_cjk_is_decoded_as_utf8(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=测试",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "修复：中文提交信息",
        ],
        cwd=tmp_path,
        check=True,
    )

    ok, output = _run_git_command(str(tmp_path), "log", "-1", "--pretty=%s")

    assert ok is True
    assert output == "修复：中文提交信息"
