"""Service-backed implementation of the cron port."""

from __future__ import annotations

from typing import Any

from openharness.services.cron import (
    delete_cron_job,
    get_cron_job,
    load_cron_jobs,
    set_job_enabled,
    upsert_cron_job,
    validate_cron_expression,
    validate_timezone,
)


class ServiceCronPort:
    """Delegate cron operations to the file-backed service."""

    def validate_expression(self, expression: str) -> bool:
        return validate_cron_expression(expression)

    def validate_timezone(self, timezone: str | None) -> bool:
        return validate_timezone(timezone)

    def upsert_job(self, job: dict[str, Any]) -> None:
        upsert_cron_job(job)

    def delete_job(self, name: str) -> bool:
        return delete_cron_job(name)

    def load_jobs(self) -> list[dict[str, Any]]:
        return load_cron_jobs()

    def set_job_enabled(self, name: str, enabled: bool) -> bool:
        return set_job_enabled(name, enabled)

    def scheduler_running(self) -> bool:
        from openharness.services.cron_scheduler import is_scheduler_running

        return is_scheduler_running()

    def get_job(self, name: str) -> dict[str, Any] | None:
        return get_cron_job(name)

    def command_for_job(self, job: dict[str, Any]) -> str:
        from openharness.services.cron_scheduler import _command_for_job

        return _command_for_job(job)
