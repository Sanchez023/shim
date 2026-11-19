"""Interval parsing helpers for the ``/loop`` slash command."""

from __future__ import annotations

import re

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def interval_to_cron(value: str) -> str:
    """Convert a human interval such as ``5m`` into a 5-field cron expression.

    The scheduler uses standard 5-field cron, so the minimum interval is one
    minute. Use ``/cron`` for arbitrary cron expressions.
    """
    match = _INTERVAL_RE.match(value or "")
    if match is None:
        raise ValueError(
            f"invalid interval: {value!r}; use forms like 1m, 5m, 2h, 1d"
        )
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise ValueError("interval must be greater than zero")
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds < 60:
        raise ValueError("the minimum loop interval is 1 minute")
    if seconds % 86400 == 0:
        days = seconds // 86400
        if days > 1:
            raise ValueError(
                "day intervals greater than 1 day do not map cleanly to cron; "
                "use 1d or a /cron expression"
            )
        return "0 0 */1 * *"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        if hours > 23:
            raise ValueError("hour interval must be between 1 and 23")
        if 24 % hours != 0:
            raise ValueError(
                f"{hours}h does not divide 24 evenly; use 1, 2, 3, 4, 6, 8, 12, "
                "or 24h, or a /cron expression"
            )
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    if seconds % 60 == 0:
        minutes = seconds // 60
        if minutes > 59:
            raise ValueError("minute interval must be between 1 and 59")
        if 60 % minutes != 0:
            raise ValueError(
                f"{minutes}m does not divide 60 evenly; use 1, 2, 3, 4, 5, 6, 10, "
                "12, 15, 20, or 30m, or a /cron expression"
            )
        return "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
    raise ValueError("interval must resolve to whole minutes")


def format_loop_summary(job: dict[str, object]) -> str:
    """Return one compact line for a loop job."""
    payload = job.get("payload")
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
    if len(message) > 80:
        message = message[:80] + "..."
    status = "on" if job.get("enabled", True) else "off"
    return (
        f"- {job.get('name', '?')} [{status}] {job.get('schedule', '?')}"
        f"  next={str(job.get('next_run') or 'n/a')[:19]}"
        f"  prompt={message or '(empty)'}"
    )


__all__ = ["format_loop_summary", "interval_to_cron"]
