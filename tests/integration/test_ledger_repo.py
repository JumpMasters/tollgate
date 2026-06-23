"""Integration tests for PostgresLedgerRepository (real Postgres, §5.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.ledger_repo import PostgresLedgerRepository
from tollgate.domain.ids import BudgetId, LedgerEntryId
from tollgate.domain.records import LedgerEntry, LedgerKind

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_budget(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b1', 'org', 'o1', 'calendar_month', 1000)"
        )
    )


async def _ledger_rows(conn: AsyncConnection) -> list[Row[tuple[object, ...]]]:
    return list(
        await conn.execute(
            text(
                "SELECT entry_id, kind, budget_id, reservation_id, delta_reserved_micro, "
                "delta_committed_micro, delta_overage_micro FROM ledger ORDER BY entry_id"
            )
        )
    )


async def test_append_writes_a_single_entry(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    repo = PostgresLedgerRepository(db_conn)
    await repo.append(
        [
            LedgerEntry(
                entry_id=LedgerEntryId("e1"),
                kind=LedgerKind.RESERVE,
                budget_id=BudgetId("b1"),
                period_start=PERIOD,
                delta_reserved_micro=100,
            )
        ]
    )
    rows = await _ledger_rows(db_conn)
    assert len(rows) == 1
    assert rows[0].kind == "reserve"
    assert rows[0].delta_reserved_micro == 100
    assert rows[0].reservation_id is None


async def test_append_writes_multiple_entries_in_one_call(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    repo = PostgresLedgerRepository(db_conn)
    await repo.append(
        [
            LedgerEntry(
                entry_id=LedgerEntryId("e1"),
                kind=LedgerKind.RESERVE,
                budget_id=BudgetId("b1"),
                period_start=PERIOD,
                delta_reserved_micro=100,
            ),
            LedgerEntry(
                entry_id=LedgerEntryId("e2"),
                kind=LedgerKind.OVERAGE,
                budget_id=BudgetId("b1"),
                period_start=PERIOD,
                delta_overage_micro=20,
            ),
        ]
    )
    rows = await _ledger_rows(db_conn)
    assert [r.entry_id for r in rows] == ["e1", "e2"]
    assert [r.kind for r in rows] == ["reserve", "overage"]
    assert rows[1].delta_overage_micro == 20


async def test_append_empty_is_a_noop(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    repo = PostgresLedgerRepository(db_conn)
    await repo.append([])
    assert await _ledger_rows(db_conn) == []


async def test_append_round_trips_all_provenance_columns(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    repo = PostgresLedgerRepository(db_conn)
    await repo.append(
        [
            LedgerEntry(
                entry_id=LedgerEntryId("e1"),
                kind=LedgerKind.COMMIT_ADJUST,
                budget_id=BudgetId("b1"),
                period_start=PERIOD,
                reservation_id=None,
                delta_committed_micro=80,
                delta_overage_micro=20,
                actual_input_tokens=1000,
                actual_output_tokens=200,
                provider="anthropic",
                price_book_version="v1",
                ref="reap-1",
            )
        ]
    )
    row = (
        await db_conn.execute(
            text(
                "SELECT period_start, actual_input_tokens, actual_output_tokens, "
                "provider, price_book_version, ref, delta_committed_micro, delta_overage_micro "
                "FROM ledger WHERE entry_id = 'e1'"
            )
        )
    ).one()
    assert row.period_start == PERIOD
    assert row.actual_input_tokens == 1000
    assert row.actual_output_tokens == 200
    assert row.provider == "anthropic"
    assert row.price_book_version == "v1"
    assert row.ref == "reap-1"
    assert row.delta_committed_micro == 80
    assert row.delta_overage_micro == 20
