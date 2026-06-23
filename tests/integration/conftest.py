"""Integration-test harness: a real Postgres via testcontainers.

Every test under ``tests/integration/`` is auto-marked ``integration`` so that
``make test`` (``-m "not integration"``) skips it without Docker. A single
session-scoped container backs the whole run; each test gets its own connection in
a transaction that is rolled back afterwards, so tests never see each other's rows.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

#: Every data table, truncated as one CASCADE so committing concurrency tests never leak rows
#: into another test (the schema itself is left intact — only rows are removed).
_DATA_TABLES = (
    "org, team, user_principal, project, api_credential, price_book, price, "
    "budget, budget_alert, budget_balance, reservation, reservation_line, "
    "ledger, idempotency_key"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test in this package ``integration`` (it needs Docker)."""
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start Postgres, migrate it to head, and yield its asyncpg URL.

    This fixture is synchronous and runs at session setup with no event loop active,
    so the async Alembic env's ``asyncio.run`` is safe here.
    """
    with PostgresContainer("postgres:17") as postgres:
        url = postgres.get_connection_url(driver="asyncpg")
        os.environ["TOLLGATE_DATABASE_URL"] = url
        config = Config()
        config.set_main_option("script_location", "migrations")
        command.upgrade(config, "head")
        yield url


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


@pytest_asyncio.fixture
async def committing_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine whose connections really commit, for cross-transaction concurrency tests.

    Unlike ``db_conn`` (one rolled-back transaction), these tests need several connections that
    genuinely commit so contention on shared rows is real. Each connection is fresh
    (``NullPool``); on teardown every data table is truncated so committed rows never leak into
    another test.
    """
    engine = create_async_engine(postgres_url, poolclass=pool.NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {_DATA_TABLES} CASCADE"))
        await engine.dispose()
