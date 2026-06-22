"""After `alembic upgrade head`, the database holds every table in the model."""

from __future__ import annotations

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import metadata


async def test_migration_creates_every_model_table(db_conn: AsyncConnection) -> None:
    def _table_names(sync_conn: Connection) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    present = await db_conn.run_sync(_table_names)
    assert set(metadata.tables) <= present
    assert "alembic_version" in present  # Alembic stamped the revision
