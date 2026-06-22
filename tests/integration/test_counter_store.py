"""Integration tests for PostgresCounterStore (real Postgres, §5.2/§5.3/§5.5)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.counter_store import PostgresCounterStore
from tollgate.domain.ids import BudgetId

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
OTHER_PERIOD = datetime(2026, 7, 1, tzinfo=UTC)


async def _seed_budget(
    conn: AsyncConnection, *, budget_id: str = "b1", scope_id: str = "o1", limit: int = 1000
) -> None:
    await conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES (:id, 'org', :sid, 'calendar_month', :lim)"
        ),
        {"id": budget_id, "sid": scope_id, "lim": limit},
    )


async def _balance(conn: AsyncConnection, budget_id: str = "b1") -> tuple[int, int, int, int]:
    row = (
        await conn.execute(
            text(
                "SELECT limit_micro, reserved_micro, committed_micro, overage_micro "
                "FROM budget_balance WHERE budget_id = :id"
            ),
            {"id": budget_id},
        )
    ).one()
    return (row.limit_micro, row.reserved_micro, row.committed_micro, row.overage_micro)


# --- ensure_period (lazy period-roll) --------------------------------------


async def test_ensure_period_seeds_row_from_budget_limit(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    assert await _balance(db_conn) == (1000, 0, 0, 0)


async def test_ensure_period_is_idempotent(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.reserve(BudgetId("b1"), PERIOD, 400)
    # A second ensure_period must NOT reset the row (ON CONFLICT DO NOTHING).
    await store.ensure_period(BudgetId("b1"), PERIOD)
    assert await _balance(db_conn) == (1000, 400, 0, 0)


async def test_ensure_period_creates_a_distinct_row_per_period(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.ensure_period(BudgetId("b1"), OTHER_PERIOD)
    count = (
        await db_conn.execute(
            text("SELECT count(*) AS n FROM budget_balance WHERE budget_id = 'b1'")
        )
    ).one()
    assert count.n == 2


# --- reserve (the invariant-guarded conditional write) ---------------------


async def test_reserve_within_headroom_succeeds(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    assert await store.reserve(BudgetId("b1"), PERIOD, 600) is True
    assert await _balance(db_conn) == (1000, 600, 0, 0)


async def test_reserve_exceeding_headroom_is_denied_and_leaves_the_row(
    db_conn: AsyncConnection,
) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    assert await store.reserve(BudgetId("b1"), PERIOD, 600) is True
    # 600 + 500 > 1000 → denied, reserved unchanged.
    assert await store.reserve(BudgetId("b1"), PERIOD, 500) is False
    assert await _balance(db_conn) == (1000, 600, 0, 0)


async def test_reserve_to_the_exact_limit_succeeds(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    assert await store.reserve(BudgetId("b1"), PERIOD, 1000) is True
    assert await _balance(db_conn) == (1000, 1000, 0, 0)


async def test_reserve_headroom_accounts_for_committed_and_overage(
    db_conn: AsyncConnection,
) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    # committed 500 + overage 300 leaves remaining = 1000 - 500 - 300 = 200.
    await db_conn.execute(
        text(
            "UPDATE budget_balance SET committed_micro = 500, overage_micro = 300 "
            "WHERE budget_id = 'b1'"
        )
    )
    assert await store.reserve(BudgetId("b1"), PERIOD, 201) is False
    assert await store.reserve(BudgetId("b1"), PERIOD, 200) is True
    assert await _balance(db_conn) == (1000, 200, 500, 300)


# --- commit (reconcile) and release ----------------------------------------


async def test_commit_moves_estimate_and_records_overage(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.reserve(BudgetId("b1"), PERIOD, 600)
    # actual 800 > est 600: committed += min(800,600)=600; overage += 200; reserved -= 600.
    await store.commit(BudgetId("b1"), PERIOD, reserved_micro=600, actual_micro=800)
    assert await _balance(db_conn) == (1000, 0, 600, 200)


async def test_commit_under_reservation_releases_the_difference(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.reserve(BudgetId("b1"), PERIOD, 600)
    # actual 400 < est 600: committed += 400; overage += 0; reserved -= 600 (frees 200).
    await store.commit(BudgetId("b1"), PERIOD, reserved_micro=600, actual_micro=400)
    assert await _balance(db_conn) == (1000, 0, 400, 0)


async def test_release_lowers_reserved(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.reserve(BudgetId("b1"), PERIOD, 600)
    await store.release(BudgetId("b1"), PERIOD, 200)
    assert await _balance(db_conn) == (1000, 400, 0, 0)


async def test_release_guard_keeps_reserved_non_negative(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn, limit=1000)
    store = PostgresCounterStore(db_conn)
    await store.ensure_period(BudgetId("b1"), PERIOD)
    await store.reserve(BudgetId("b1"), PERIOD, 200)
    # Releasing more than is held matches zero rows: reserved is left unchanged.
    await store.release(BudgetId("b1"), PERIOD, 500)
    assert await _balance(db_conn) == (1000, 200, 0, 0)
