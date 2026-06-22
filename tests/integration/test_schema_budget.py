"""Constraint tests for budget / budget_alert / budget_balance (the §3 storage invariant)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
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


async def test_budget_unique_rejects_duplicate_scope_period(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO budget "
                "(budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
                "VALUES ('b2', 'org', 'o1', 'calendar_month', 2000)"
            )
        )


async def test_budget_period_kind_check_rejects_unknown(db_conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO budget "
                "(budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
                "VALUES ('b1', 'org', 'o1', 'fortnight', 1000)"
            )
        )


async def test_budget_balance_accepts_a_valid_row(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    await db_conn.execute(
        text(
            "INSERT INTO budget_balance "
            "(budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
            "overage_micro) "
            "VALUES ('b1', now(), 1000, 600, 400, 0)"
        )
    )


async def test_budget_balance_rejects_negative_reserved(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO budget_balance "
                "(budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
                "overage_micro) "
                "VALUES ('b1', now(), 1000, -1, 0, 0)"
            )
        )


async def test_budget_balance_rejects_reserved_plus_committed_over_limit(
    db_conn: AsyncConnection,
) -> None:
    await _seed_budget(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO budget_balance "
                "(budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
                "overage_micro) "
                "VALUES ('b1', now(), 1000, 600, 500, 0)"
            )
        )


async def test_budget_balance_allows_overage_beyond_limit(db_conn: AsyncConnection) -> None:
    # overage is OUTSIDE the reserved+committed<=limit CHECK (committed alone never
    # exceeds limit), so a row with committed==limit and large overage is valid (§3).
    await _seed_budget(db_conn)
    await db_conn.execute(
        text(
            "INSERT INTO budget_balance "
            "(budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
            "overage_micro) "
            "VALUES ('b1', now(), 1000, 0, 1000, 500)"
        )
    )


async def test_budget_alert_threshold_check_rejects_out_of_range(db_conn: AsyncConnection) -> None:
    await _seed_budget(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text("INSERT INTO budget_alert (budget_id, threshold_pct) VALUES ('b1', 150)")
        )
