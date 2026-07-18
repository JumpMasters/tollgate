"""Typed application settings, loaded from the environment.

Defaults are development-friendly; deployments override them through environment
variables prefixed ``TOLLGATE_``.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the control plane."""

    model_config = SettingsConfigDict(env_prefix="TOLLGATE_", env_file=".env", extra="ignore")

    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://tollgate:tollgate@localhost:5432/tollgate"),
        description="Async SQLAlchemy URL for the Postgres ledger of record.",
    )
    reserve_statement_timeout_ms: int = Field(
        default=2_000,
        ge=1,
        description="Server-side statement timeout for the synchronous reserve path.",
    )
    reservation_ttl_seconds: int = Field(
        default=600,
        ge=1,
        description="Default reservation TTL before the reaper may release it.",
    )
    token_hash_secret: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Keyed-hash secret (server pepper) for bearer-token hashing (ADR 0026). "
            "Must be set for the app to start; there is no usable default."
        ),
    )
    reaper_poll_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Seconds the reservation reaper waits between ticks (§5.5).",
    )
    reaper_batch_size: int = Field(
        default=100,
        ge=1,
        description="Max reservations the reservation reaper reaps per tick (bounded work).",
    )
    idempotency_ttl_hours: int = Field(
        default=24,
        ge=1,
        description="Age at which idempotency keys become reapable (§5.5).",
    )
    idempotency_reaper_poll_interval_seconds: float = Field(
        default=3600.0,
        gt=0,
        description="Seconds the idempotency reaper waits between ticks (§5.5).",
    )
    idempotency_reaper_batch_size: int = Field(
        default=500,
        ge=1,
        description="Rows deleted per idempotency-reaper batch (bounded per-tx work).",
    )


def load_settings() -> Settings:
    """Load settings from the environment."""
    return Settings()
