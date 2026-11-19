"""Desktop completion notifications.

Windows uses a PowerShell NotifyIcon balloon (works without an app
registration and appears in the Windows notification center); macOS uses
AppleScript; Linux uses ``notify-send`` when available. Every failure is
silent: notifications must never break an agent turn.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from openharness.config.settings import NotificationSettings

log = logging.getLogger(__name__)


def format_completion_body(
    text: str,
    *,
    duration_seconds: float | None = None,
    max_chars: int = 180,
) -> str:
    """Build a compact notification body from the last assistant message."""
    normalized = " ".join((text or "").split())
    suffix = (
        f" · {int(duration_seconds)}s"
        if duration_seconds is not None and duration_seconds >= 0
        else ""
    )
    allowance = max(0, max_chars - len(suffix))
    if len(normalized) > allowance:
        if allowance >= 3:
            normalized = normalized[: allowance - 3].rstrip() + "..."
        else:
            normalized = normalized[:allowance]
    result = f"{normalized}{suffix}" if normalized else ("Turn completed." + suffix)
    return result[:max_chars]


def send_notification(title: str, body: str) -> bool:
    """Send one best-effort desktop notification."""
    try:
        if sys.platform == "win32":
            return _windows_notification(title, body)
        if sys.platform == "darwin":
            return _macos_notification(title, body)
        return _linux_notification(title, body)
    except Exception:
        log.debug("desktop notification failed", exc_info=True)
        return False


def send_completion_notification(
    title: str,
    body: str,
    *,
    settings: NotificationSettings,
    duration_seconds: float | None = None,
) -> bool:
    """Send a turn-complete notification when enabled by settings."""
    if not settings.enabled or not settings.on_turn_complete:
        return False
    if (
        duration_seconds is not None
        and duration_seconds < settings.min_duration_seconds
    ):
        return False
    return send_notification(title, format_completion_body(body, duration_seconds=duration_seconds))


def _windows_notification(title: str, body: str) -> bool:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return False
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(6000,$env:SHIM_NOTIFY_TITLE,$env:SHIM_NOTIFY_BODY,"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 6500;"
        "$n.Dispose()"
    )
    env = {
        **os.environ,
        "SHIM_NOTIFY_TITLE": title,
        "SHIM_NOTIFY_BODY": body,
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
    )
    return True


def _macos_notification(title: str, body: str) -> bool:
    osascript = shutil.which("osascript")
    if osascript is None:
        return False

    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {quote(body)} with title {quote(title)}"
    subprocess.Popen(
        [osascript, "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _linux_notification(title: str, body: str) -> bool:
    notify_send = shutil.which("notify-send")
    if notify_send is None:
        return False
    subprocess.Popen(
        [notify_send, title, body],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


__all__ = [
    "format_completion_body",
    "send_completion_notification",
    "send_notification",
]
