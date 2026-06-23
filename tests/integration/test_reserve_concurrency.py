"""Cross-transaction reserve races on real Postgres (§5.2/§5.3).

These commit from several connections at once to prove what a single rolled-back connection
cannot: under contention exactly the nodes' headroom-many reserves succeed (most-restrictive),
and overlapping reserves by sibling users on shared parent rows never deadlock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.reserve_tx import PostgresReserveTransaction
from tollgate.domain.ids import BudgetId
from tollgate.domain.scopes import BudgetNode, ScopeKind

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_budget(
    engine: AsyncEngine, *, budget_id: str, scope_kind: str, scope_id: str, limit: int
) -> None:
    """Create a budget and its current-period balance row (committed), ready to contend on."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO budget"
                " (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro)"
                " VALUES (:b, :k, :s, 'calendar_month', :lim)"
            ),
            {"b": budget_id, "k": scope_kind, "s": scope_id, "lim": limit},
        )
        await conn.execute(
            text(
                "INSERT INTO budget_balance (budget_id, period_start, limit_micro) "
                "VALUES (:b, :p, :lim)"
            ),
            {"b": budget_id, "p": PERIOD, "lim": limit},
        )


async def _attempt_reserve(engine: AsyncEngine, nodes: list[BudgetNode], amount_micro: int) -> bool:
    """Own transaction: commit on success, roll back on denial or unexpected error."""
    async with engine.connect() as conn:
        txn = await conn.begin()
        try:
            outcome = await PostgresReserveTransaction(conn).reserve(nodes, PERIOD, amount_micro)
        except Exception:
            await txn.rollback()
            raise
        if outcome.ok:
            await txn.commit()
        else:
            await txn.rollback()
        return outcome.ok


async def _reserved(engine: AsyncEngine, budget_id: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT reserved_micro FROM budget_balance WHERE budget_id = :b"),
                    {"b": budget_id},
                )
            ).scalar_one()
        )


async def test_concurrent_reserves_admit_exactly_the_headroom(
    committing_engine: AsyncEngine,
) -> None:
    # limit 1000, estimate 300 → at most 3 of 8 concurrent reserves fit (3 * 300 = 900 ≤ 1000).
    await _seed_budget(
        committing_engine, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1000
    )
    node = [BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")]
    results = await asyncio.gather(
        *(_attempt_reserve(committing_engine, node, 300) for _ in range(8))
    )
    # The guarded WHERE lets exactly the fitting reserves through (serialized by the row lock);
    # the rest fail definitively with no retry. committed ≤ limit holds by construction.
    assert sum(1 for ok in results if ok) == 3
    assert await _reserved(committing_engine, "b-org") == 900
