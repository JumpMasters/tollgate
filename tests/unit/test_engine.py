"""Tests for async engine construction (no database connection is opened)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.engine import build_engine


def test_build_engine_returns_async_engine() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/tollgate")
    assert isinstance(engine, AsyncEngine)
    assert engine.url.database == "tollgate"


def test_build_engine_accepts_a_custom_timeout() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost/db", statement_timeout_ms=500)
    assert isinstance(engine, AsyncEngine)


def test_build_engine_applies_pool_and_connect_settings() -> None:
    # Pool sizing and checkout/connect timeouts are configurable so checkout and connect
    # failures fail as fast as statement failures, not on SQLAlchemy's 30s default (#76).
    engine = build_engine(
        "postgresql+asyncpg://u:p@localhost/db",
        pool_size=7,
        max_overflow=3,
        pool_timeout_seconds=5.0,
        connect_timeout_seconds=2.0,
    )
    pool = engine.sync_engine.pool
    assert pool.size() == 7  # type: ignore[attr-defined]  # QueuePool
    assert pool._max_overflow == 3  # type: ignore[attr-defined]  # QueuePool internal
    assert pool._timeout == 5.0  # type: ignore[attr-defined]  # QueuePool internal
