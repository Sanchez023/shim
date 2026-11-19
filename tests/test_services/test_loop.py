"""Tests for /loop interval parsing."""

from __future__ import annotations

import pytest

from openharness.services.loop import interval_to_cron


def test_interval_to_cron() -> None:
    assert interval_to_cron("1m") == "* * * * *"
    assert interval_to_cron("5m") == "*/5 * * * *"
    assert interval_to_cron("30m") == "*/30 * * * *"
    assert interval_to_cron("1h") == "0 * * * *"
    assert interval_to_cron("2h") == "0 */2 * * *"
    assert interval_to_cron("1d") == "0 0 */1 * *"


def test_interval_rejects_seconds_and_bad_input() -> None:
    with pytest.raises(ValueError, match="minimum loop interval"):
        interval_to_cron("30s")
    with pytest.raises(ValueError, match="invalid interval"):
        interval_to_cron("soon")


def test_interval_rejects_uneven_cron_intervals() -> None:
    with pytest.raises(ValueError, match="does not divide 60 evenly"):
        interval_to_cron("45m")
    with pytest.raises(ValueError, match="does not divide 24 evenly"):
        interval_to_cron("7h")
    with pytest.raises(ValueError, match="day intervals greater than 1 day"):
        interval_to_cron("3 days")
