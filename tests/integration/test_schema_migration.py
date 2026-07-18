"""After `alembic upgrade head`, the database matches the canonical model exactly.

Comparing only table names would miss column, index, and constraint drift between the
migration path and ``tollgate.adapters.postgres.schema`` — exactly the drift that appears
once the baseline is frozen into explicit DDL and later edited out of step with the model
(#64). These tests reflect the migrated database and assert it is the model, in full.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, Connection, inspect
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import metadata


async def test_migration_creates_every_model_table(db_conn: AsyncConnection) -> None:
    def _table_names(sync_conn: Connection) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    present = await db_conn.run_sync(_table_names)
    # Exact match (plus Alembic's own bookkeeping table) catches drift in either
    # direction: a model table missing from the migration, or a migrated table
    # absent from the model.
    assert present == set(metadata.tables) | {"alembic_version"}


async def test_migration_matches_the_model_schema(db_conn: AsyncConnection) -> None:
    def _diff(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(sync_conn)
        return list(compare_metadata(context, metadata))

    diff = await db_conn.run_sync(_diff)
    # Alembic's own comparator normalizes types and index/unique reflection quirks, so an
    # empty diff means the migrated schema is columns-for-columns and indexes-for-indexes the
    # model. A frozen baseline that drifts from the model shows up here as a non-empty diff.
    assert diff == [], f"schema drift between migrations and model: {diff}"


async def test_migration_preserves_every_check_constraint(db_conn: AsyncConnection) -> None:
    # compare_metadata does not diff CHECK constraints, so pin them by name per table: a frozen
    # baseline that dropped or renamed one (e.g. a balance non-negativity guard) is caught here.
    expected = {
        table.name: {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
        for table in metadata.tables.values()
    }
    expected = {name: checks for name, checks in expected.items() if checks}

    def _reflected(sync_conn: Connection) -> dict[str, set[str]]:
        inspector = inspect(sync_conn)
        return {
            name: {cc["name"] for cc in inspector.get_check_constraints(name) if cc["name"]}
            for name in expected
        }

    reflected = await db_conn.run_sync(_reflected)
    assert reflected == expected
