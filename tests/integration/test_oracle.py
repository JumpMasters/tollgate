"""The oracle reader over real Postgres: clean data passes; a corrupted balance is caught."""

from __future__ import annotations

from datetime import UTC, datetime

from loadtest.oracle import Check, load_and_check
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.schema import (
    budget,
    budget_balance,
    ledger,
    org,
    team,
    user_principal,
)

_P = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_rollup_tree(engine: AsyncEngine) -> None:
    """Org→team→two users, all budgeted, committed rolling up exactly (team 30 == 20 + 10)."""
    async with engine.begin() as conn:
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="T1"))
        await conn.execute(
            user_principal.insert().values(
                [
                    {"user_id": "u1", "team_id": "t1", "external_ref": None},
                    {"user_id": "u2", "team_id": "t1", "external_ref": None},
                ]
            )
        )
        for bid, sk, sid, lim, committed in (
            ("b-org", "org", "o1", 100000, 30),
            ("b-t1", "team", "t1", 5000, 30),
            ("b-u1", "user", "u1", 1000, 20),
            ("b-u2", "user", "u2", 1000, 10),
        ):
            await conn.execute(
                budget.insert().values(
                    budget_id=bid,
                    scope_kind=sk,
                    scope_id=sid,
                    period_kind="calendar_month",
                    hard_limit_micro=lim,
                )
            )
            await conn.execute(
                budget_balance.insert().values(
                    budget_id=bid,
                    period_start=_P,
                    limit_micro=lim,
                    committed_micro=committed,
                )
            )
            # one commit ledger row so conservation reconstructs committed exactly
            await conn.execute(
                ledger.insert().values(
                    entry_id=f"e-{bid}",
                    kind="commit_adjust",
                    budget_id=bid,
                    period_start=_P,
                    reservation_id=None,
                    delta_committed_micro=committed,
                )
            )


async def test_reader_passes_on_a_clean_ledger(committing_engine: AsyncEngine) -> None:
    await _seed_rollup_tree(committing_engine)
    async with committing_engine.connect() as conn:
        report = await load_and_check(conn)
    assert report.ok, report.violations


async def test_reader_flags_a_corrupted_balance(committing_engine: AsyncEngine) -> None:
    await _seed_rollup_tree(committing_engine)
    async with committing_engine.begin() as conn:
        # inflate committed on b-u1 without a matching ledger row: breaks conservation AND roll-up
        await conn.execute(
            text(
                "UPDATE budget_balance SET committed_micro = committed_micro + 7 "
                "WHERE budget_id = 'b-u1'"
            )
        )
    async with committing_engine.connect() as conn:
        report = await load_and_check(conn)
    assert not report.ok
    checks = {v.check for v in report.violations}
    assert Check.CONSERVATION in checks
    assert Check.TREE_ROLLUP in checks


async def test_reader_can_run_a_check_subset(committing_engine: AsyncEngine) -> None:
    await _seed_rollup_tree(committing_engine)
    async with committing_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE budget_balance SET committed_micro = committed_micro + 7 "
                "WHERE budget_id = 'b-u1'"
            )
        )
    async with committing_engine.connect() as conn:
        report = await load_and_check(conn, checks=frozenset({Check.NON_NEGATIVE}))
    assert report.ok  # only non-negativity ran, and everything is still non-negative
