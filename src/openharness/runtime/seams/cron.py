"""Cron job port used by the model-facing cron tools."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CronPort(Protocol):
    """Read/write cron jobs and validate schedules."""

    def validate_expression(self, expression: str) -> bool:
        """Return True for a valid 5-field cron expression."""

    def validate_timezone(self, timezone: str | None) -> bool:
        """Return True for a valid IANA timezone (or an empty value)."""

    def upsert_job(self, job: dict[str, Any]) -> None:
        """Insert or replace one job."""

    def delete_job(self, name: str) -> bool:
        """Delete one job; return False when it does not exist."""

    def load_jobs(self) -> list[dict[str, Any]]:
        """Return all configured jobs."""

    def set_job_enabled(self, name: str, enabled: bool) -> bool:
        """Enable/disable a job; return False when it does not exist."""

    def scheduler_running(self) -> bool:
        """Return whether the scheduler daemon is running."""

    def get_job(self, name: str) -> dict[str, Any] | None:
        """Return one job by name."""

    def command_for_job(self, job: dict[str, Any]) -> str:
        """Return the shell command used to execute a job."""
