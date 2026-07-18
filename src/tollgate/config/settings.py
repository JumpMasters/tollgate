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


def load_settings() -> Settings:
    """Load settings from the environment."""
    return Settings()
