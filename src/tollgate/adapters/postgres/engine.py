"""Async SQLAlchemy engine construction.

Engine creation is lazy — it does not open a connection — so it is safe to build
at composition time and to exercise without a running database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def build_engine(
    database_url: str,
    *,
    statement_timeout_ms: int = 2_000,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout_seconds: float = 10.0,
    connect_timeout_seconds: float = 10.0,
) -> AsyncEngine:
    """Create an async engine with bounded statement, checkout, and connect timeouts.

    Fail-fast has to cover more than statement execution: under pool exhaustion a call
    otherwise blocks up to SQLAlchemy's 30s checkout default, and asyncpg's connect timeout
    applies before any statement runs. All three bounds — the server-side ``statement_timeout``,
    the pool ``pool_timeout``, and the asyncpg ``connect`` timeout — plus pool sizing are set
    here so the slowest failure modes the reserve path cares about fail as fast as a slow query
    (#76).
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_seconds,
        connect_args={
            "timeout": connect_timeout_seconds,
            "server_settings": {"statement_timeout": str(statement_timeout_ms)},
        },
    )
