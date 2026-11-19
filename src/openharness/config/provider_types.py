"""Provider profile and resolved-auth value types."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


class ProviderProfile(BaseModel):
    """Named provider workflow configuration."""

    label: str
    provider: str
    api_format: str
    auth_source: str
    default_model: str
    base_url: str | None = None
    last_model: str | None = None
    credential_slot: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None

    @property
    def resolved_model(self) -> str:
        """Return the active model for this profile."""
        from openharness.config.providers import resolve_model_setting

        return resolve_model_setting(
            (self.last_model or "").strip() or self.default_model,
            self.provider,
            default_model=self.default_model,
        )


@dataclass(frozen=True)
class ResolvedAuth:
    """Normalized auth material used to construct API clients."""

    provider: str
    auth_kind: str
    value: str
    source: str
    state: str = "configured"
