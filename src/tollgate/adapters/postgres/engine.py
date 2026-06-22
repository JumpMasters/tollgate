"""Async SQLAlchemy engine construction.

Engine creation is lazy — it does not open a connection — so it is safe to build
at composition time and to exercise without a running database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def build_engine(database_url: str, *, statement_timeout_ms: int = 2_000) -> AsyncEngine:
    """Create an async engine with a bounded server-side statement timeout.

    The statement timeout keeps the synchronous reserve path failing *fast* when
    the datastore is slow, rather than adding latency to every call.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": str(statement_timeout_ms)}},
    )
