"""Smoke test: the integration harness can reach a real Postgres."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def test_database_is_reachable(db_conn: AsyncConnection) -> None:
    result = await db_conn.execute(text("SELECT 1"))
    assert result.scalar_one() == 1
