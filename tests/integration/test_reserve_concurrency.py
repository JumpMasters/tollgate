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


async def test_sibling_reserves_serialize_on_shared_parents_without_deadlock(
    committing_engine: AsyncEngine,
) -> None:
    # Two sibling users share BOTH parent rows (org and team). The team is the tightest parent
    # (limit 1000); org and the users are roomy. Every reserve must take org (rank 0) then team
    # (rank 1) then its own user (rank 2) — two SHARED rows acquired in one canonical order, so
    # no overlapping pair can form a lock cycle. Without deterministic ordering this is the
    # classic sibling deadlock; with it, the gather simply completes.
    await _seed_budget(
        committing_engine, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1_000_000
    )
    await _seed_budget(
        committing_engine, budget_id="b-team", scope_kind="team", scope_id="tm", limit=1000
    )
    await _seed_budget(
        committing_engine, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000_000
    )
    await _seed_budget(
        committing_engine, budget_id="b-u2", scope_kind="user", scope_id="u2", limit=1_000_000
    )
    org = BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")
    team = BudgetNode(BudgetId("b-team"), ScopeKind.TEAM, "tm")
    # Each principal passes its set in a different, scrambled input order — reserve_tx sorts.
    set_a = [BudgetNode(BudgetId("b-u1"), ScopeKind.USER, "u1"), team, org]
    set_b = [org, BudgetNode(BudgetId("b-u2"), ScopeKind.USER, "u2"), team]
    results = await asyncio.gather(
        *(
            _attempt_reserve(committing_engine, set_a if i % 2 == 0 else set_b, 200)
            for i in range(8)
        )
    )
    # The team (1000 / 200 = 5) binds across both siblings, most-restrictive.
    assert sum(1 for ok in results if ok) == 5
    team_reserved = await _reserved(committing_engine, "b-team")
    org_reserved = await _reserved(committing_engine, "b-org")
    u1_reserved = await _reserved(committing_engine, "b-u1")
    u2_reserved = await _reserved(committing_engine, "b-u2")
    assert team_reserved == 1000  # the tightest shared parent binds
    # All-or-nothing: every success reserved org + team + exactly one user line by the estimate,
    # and every denial reserved none — so all three counts agree.
    assert org_reserved == 1000
    assert u1_reserved + u2_reserved == 1000


async def test_concurrent_first_reservers_converge_on_one_period_balance_row(
    committing_engine: AsyncEngine,
) -> None:
    """``ensure_period``'s ``ON CONFLICT DO NOTHING`` under a real cross-transaction race (§5.3).

    Seed only the ``budget`` row for a fresh period -- no ``budget_balance`` row exists yet --
    then fire N concurrent reserves that all race to INSERT it. Exactly one balance row must
    survive the race (the conflicting INSERTs no-op rather than error), and every admitted
    reserve's amount must land on that single surviving row.
    """
    async with committing_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO budget"
                " (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro)"
                " VALUES (:b, :k, :s, 'calendar_month', :lim)"
            ),
            {"b": "b-fresh", "k": "org", "s": "o-fresh", "lim": 100_000},
        )
    node = [BudgetNode(BudgetId("b-fresh"), ScopeKind.ORG, "o-fresh")]
    estimate = 100
    results = await asyncio.gather(
        *(_attempt_reserve(committing_engine, node, estimate) for _ in range(8))
    )
    admitted_count = sum(1 for ok in results if ok)
    assert admitted_count == 8  # headroom (100,000) comfortably covers all 8 racers
    async with committing_engine.connect() as conn:
        balance_rows = (
            await conn.execute(text("SELECT count(*) FROM budget_balance"))
        ).scalar_one()
    assert balance_rows == 1
    assert await _reserved(committing_engine, "b-fresh") == admitted_count * estimate
