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
