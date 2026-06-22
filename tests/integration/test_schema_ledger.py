"""Constraint tests for the append-only ledger and the idempotency-key table."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection


async def _seed_budget(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "INSERT INTO budget "
            "(budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b1', 'org', 'o1', 'calendar_month', 1000)"
        )
    )


async def test_ledger_kind_check_rejects_unknown(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO ledger "
                "(entry_id, kind, budget_id, period_start, delta_reserved_micro, "
                " delta_committed_micro, delta_overage_micro) "
                "VALUES ('e1', 'teleport', 'b1', now(), 100, 0, 0)"
            )
        )


async def test_ledger_accepts_a_reserve_row_without_a_reservation(db_conn: AsyncConnection) -> None:
    # reservation_id is nullable (e.g. grace_backfill); a valid kind + budget inserts.
    await _seed_budget(db_conn)
    await db_conn.execute(
        text(
            "INSERT INTO ledger "
            "(entry_id, kind, budget_id, period_start, delta_reserved_micro, "
            " delta_committed_micro, delta_overage_micro) "
            "VALUES ('e1', 'reserve', 'b1', now(), 100, 0, 0)"
        )
    )


async def test_ledger_has_budget_ts_index(db_conn: AsyncConnection) -> None:
    def _index_names(sync_conn: Connection) -> set[str | None]:
        return {ix["name"] for ix in inspect(sync_conn).get_indexes("ledger")}

    names = await db_conn.run_sync(_index_names)
    assert "ix_ledger_budget_id_ts" in names


async def test_idempotency_key_primary_key_rejects_duplicate(db_conn: AsyncConnection) -> None:
    await db_conn.execute(
        text("INSERT INTO idempotency_key (key, command_fingerprint) VALUES ('k1', 'fp')")
    )
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text("INSERT INTO idempotency_key (key, command_fingerprint) VALUES ('k1', 'fp2')")
        )
