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
    worker_statement_timeout_ms: int = Field(
        default=30_000,
        ge=1,
        description=(
            "Server-side statement timeout for reaper/worker maintenance queries, tuned "
            "independently of the reserve hot path (a batch scan is not the 2s reserve budget)."
        ),
    )
    db_pool_size: int = Field(
        default=5, ge=1, description="Base size of the SQLAlchemy connection pool."
    )
    db_max_overflow: int = Field(
        default=10, ge=0, description="Connections allowed beyond the pool size under load."
    )
    db_pool_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Seconds to wait for a pooled connection before failing. Fail-fast under pool "
            "exhaustion rather than SQLAlchemy's 30s default (#76)."
        ),
    )
    db_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Seconds to wait to establish a new database connection before failing.",
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
        description="Seconds the reservation reaper waits between ticks.",
    )
    reaper_batch_size: int = Field(
        default=100,
        ge=1,
        description="Max reservations the reservation reaper reaps per tick (bounded work).",
    )
    reaper_max_reap_attempts: int = Field(
        default=5,
        ge=1,
        description=(
            "Consecutive failed reap attempts before the reservation reaper quarantines a poison "
            "row — excluding it from future claims and logging an error — so it stops "
            "recirculating at the queue head and stranding its estimate unseen (#91)."
        ),
    )
    idempotency_ttl_hours: int = Field(
        default=24,
        ge=1,
        description="Age at which idempotency keys become reapable.",
    )
    idempotency_reaper_poll_interval_seconds: float = Field(
        default=3600.0,
        gt=0,
        description="Seconds the idempotency reaper waits between ticks.",
    )
    worker_max_consecutive_failures: int = Field(
        default=10,
        ge=1,
        description=(
            "Consecutive tick failures before a worker exits non-zero for the orchestrator to "
            "restart and alert, rather than failing every tick while looking healthy (#75)."
        ),
    )
    worker_backoff_base_seconds: float = Field(
        default=1.0,
        gt=0,
        description="Base of the exponential backoff a worker waits after a failed tick (#75).",
    )
    worker_backoff_max_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Ceiling for the worker failure backoff (#75).",
    )
    idempotency_reaper_batch_size: int = Field(
        default=500,
        ge=1,
        description="Rows deleted per idempotency-reaper batch (bounded per-tx work).",
    )
    idempotency_reaper_max_batches_per_tick: int = Field(
        default=100,
        ge=1,
        description=(
            "Max batches one idempotency-reaper tick drains before yielding to the next tick, "
            "so a large backlog cannot run an unbounded tick that starves graceful shutdown (#73)."
        ),
    )


def load_settings() -> Settings:
    """Load settings from the environment."""
    return Settings()
