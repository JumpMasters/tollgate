"""Alembic migration environment (async, asyncpg).

Migrations run against the same async engine the application uses; the URL comes
from ``Settings`` (env ``TOLLGATE_DATABASE_URL``). ``target_metadata`` is the
canonical schema in ``tollgate.adapters.postgres.schema``, so ``--autogenerate``
diffs each new migration against it. The baseline is frozen explicit DDL (not
``metadata.create_all``); a migration test asserts the migrated database still
equals the model, so drift is caught in CI rather than on a fresh install (#64).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from tollgate.adapters.postgres.schema import metadata
from tollgate.config.settings import load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_url() -> str:
    return load_settings().database_url.get_secret_value()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
