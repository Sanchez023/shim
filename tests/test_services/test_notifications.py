"""Tests for desktop completion notifications."""

from __future__ import annotations

from openharness.config.settings import NotificationSettings
from openharness.notifications import (
    format_completion_body,
    send_completion_notification,
    send_notification,
)


def test_format_completion_body_truncates_and_adds_duration() -> None:
    body = format_completion_body("a" * 400, duration_seconds=12.4, max_chars=50)
    assert len(body) <= 50
    assert "..." in body
    assert body.endswith("12s")
    assert "12s" in format_completion_body("done", duration_seconds=12.4)


def test_notification_respects_settings(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "openharness.notifications.send_notification",
        lambda title, body: calls.append((title, body)) or True,
    )

    assert (
        send_completion_notification(
            "Shim",
            "done",
            settings=NotificationSettings(enabled=False),
        )
        is False
    )
    assert calls == []

    assert send_completion_notification(
        "Shim",
        "done",
        settings=NotificationSettings(enabled=True, min_duration_seconds=10),
        duration_seconds=5,
    ) is False
    assert calls == []

    assert send_completion_notification(
        "Shim",
        "done",
        settings=NotificationSettings(enabled=True, min_duration_seconds=1),
        duration_seconds=5,
    ) is True
    assert calls == [("Shim", "done · 5s")]


def test_windows_notification_uses_notify_icon(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(
        "openharness.notifications.shutil.which",
        lambda name: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if name in {"powershell", "pwsh"}
        else None,
    )
    monkeypatch.setattr("openharness.notifications.subprocess.Popen", _FakePopen)

    assert send_notification("Shim", "done") is True
    assert captured["args"][0].endswith("powershell.exe")
    kwargs = captured["kwargs"]
    assert kwargs["env"]["SHIM_NOTIFY_TITLE"] == "Shim"
    assert kwargs["env"]["SHIM_NOTIFY_BODY"] == "done"
    assert "ShowBalloonTip" in captured["args"][-1]
