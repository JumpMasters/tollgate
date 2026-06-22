"""Integration-test harness: a real Postgres via testcontainers.

Every test under ``tests/integration/`` is auto-marked ``integration`` so that
``make test`` (``-m "not integration"``) skips it without Docker. A single
session-scoped container backs the whole run; each test gets its own connection in
a transaction that is rolled back afterwards, so tests never see each other's rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from testcontainers.postgres import PostgresContainer


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test in this package ``integration`` (it needs Docker)."""
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start Postgres for the session and yield its asyncpg URL."""
    with PostgresContainer("postgres:17") as postgres:
        yield postgres.get_connection_url(driver="asyncpg")


@pytest_asyncio.fixture
async def db_conn(postgres_url: str) -> AsyncIterator[AsyncConnection]:
    """A connection in a transaction that is rolled back after the test (isolation)."""
    engine = create_async_engine(postgres_url, poolclass=pool.NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
