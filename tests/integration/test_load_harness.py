"""The comparative load harness (§7): strategy semantics, the bug→fix→proof shootout, and the
product-path proof under real concurrency."""

from __future__ import annotations

from datetime import UTC, datetime

from loadtest.harness import (
    GuardedStrategy,
    HarnessTree,
    NaiveStrategy,
    OccStrategy,
    ReserveStrategy,
    _drop_harness_balance,
    _ensure_harness_balance,
    _read_harness_balances,
    _seed_harness_balance,
    run_product_workload,
    run_strategy,
)
from sqlalchemy.ext.asyncio import AsyncEngine

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


def _tree(parent_limit: int, child_limit: int = 10_000) -> HarnessTree:
    return HarnessTree(
        parent_id="p",
        parent_limit=parent_limit,
        child_ids=("c0", "c1"),
        child_limit=child_limit,
        period=_PERIOD,
    )


async def _reserved(engine: AsyncEngine, budget_id: str) -> int:
    async with engine.connect() as conn:
        balances = await _read_harness_balances(conn)
    return balances[(budget_id, _PERIOD)].reserved_micro


async def _one_reserve(
    engine: AsyncEngine, strategy: ReserveStrategy, nodes: list[str], amount: int
) -> bool:
    async with engine.connect() as conn:
        txn = await conn.begin()
        try:
            outcome = await strategy.reserve(conn, nodes, _PERIOD, amount)
        except Exception:
            await txn.rollback()
            raise
        if outcome.admitted:
            await txn.commit()
        else:
            await txn.rollback()
        return outcome.admitted


async def test_guarded_denies_when_headroom_is_short(committing_engine: AsyncEngine) -> None:
    async with committing_engine.begin() as conn:
        await _ensure_harness_balance(conn)
        await _seed_harness_balance(conn, _tree(parent_limit=100))
    try:
        assert await _one_reserve(committing_engine, GuardedStrategy(), ["p", "c0"], 60) is True
        # second 60 would push the parent to 120 > 100 -> guarded denies, nothing persists
        assert await _one_reserve(committing_engine, GuardedStrategy(), ["p", "c0"], 60) is False
        assert await _reserved(committing_engine, "p") == 60
    finally:
        await _drop_harness_balance(committing_engine)


async def test_naive_is_correct_in_isolation(committing_engine: AsyncEngine) -> None:
    # Without contention naive behaves exactly like the others: it admits a fitting reserve and
    # denies one that would not fit. Its check-then-act flaw is a CONCURRENCY phenomenon — two
    # sequential calls cannot exhibit it (the second SELECT sees the first's committed write under
    # READ COMMITTED), so the over-admission breach is demonstrated in the shootout (Task 2), not
    # here. This test pins naive's single-connection semantics as the baseline the guard must match.
    async with committing_engine.begin() as conn:
        await _ensure_harness_balance(conn)
        await _seed_harness_balance(conn, _tree(parent_limit=100))
    try:
        assert await _one_reserve(committing_engine, NaiveStrategy(), ["p", "c0"], 60) is True
        assert await _one_reserve(committing_engine, NaiveStrategy(), ["p", "c0"], 60) is False
        assert await _reserved(committing_engine, "p") == 60
    finally:
        await _drop_harness_balance(committing_engine)


async def test_occ_admits_uncontended_and_denies_when_full(committing_engine: AsyncEngine) -> None:
    async with committing_engine.begin() as conn:
        await _ensure_harness_balance(conn)
        await _seed_harness_balance(conn, _tree(parent_limit=100))
    try:
        assert await _one_reserve(committing_engine, OccStrategy(), ["p", "c0"], 60) is True
        assert await _one_reserve(committing_engine, OccStrategy(), ["p", "c0"], 60) is False
        assert await _reserved(committing_engine, "p") == 60
    finally:
        await _drop_harness_balance(committing_engine)


def _hot_tree() -> HarnessTree:
    # parent headroom (1000) is far below total demand (32 workers x up to 4 reserves x ~100),
    # so an unguarded admission policy over-admits the shared parent.
    return HarnessTree(
        parent_id="p",
        parent_limit=1000,
        child_ids=tuple(f"c{i}" for i in range(8)),
        child_limit=100_000,
        period=_PERIOD,
    )


async def test_guarded_never_overspends(committing_engine: AsyncEngine) -> None:
    try:
        metrics = await run_strategy(
            committing_engine,
            GuardedStrategy(),
            tree=_hot_tree(),
            concurrency=32,
            ops_per_worker=4,
            seed=1,
        )
        assert metrics.overspend_micro == 0
        assert metrics.violations == ()  # oracle balance-tier checks clean
    finally:
        await _drop_harness_balance(committing_engine)


async def test_naive_breaches_the_parent(committing_engine: AsyncEngine) -> None:
    try:
        metrics = await run_strategy(
            committing_engine,
            NaiveStrategy(),
            tree=_hot_tree(),
            concurrency=32,
            ops_per_worker=4,
            seed=1,
        )
        # the demonstration: an unguarded admission policy over-admits, and the oracle catches it
        assert metrics.overspend_micro > 0
        assert "storage_guard" in metrics.violations
    finally:
        await _drop_harness_balance(committing_engine)


async def test_occ_is_correct_but_thrashes(committing_engine: AsyncEngine) -> None:
    try:
        metrics = await run_strategy(
            committing_engine,
            OccStrategy(),
            tree=_hot_tree(),
            concurrency=32,
            ops_per_worker=4,
            seed=1,
        )
        assert metrics.overspend_micro == 0  # correct
        assert metrics.violations == ()
        assert metrics.retries > 0  # but the hot row thrashes
    finally:
        await _drop_harness_balance(committing_engine)


async def test_product_path_stays_correct_under_concurrency(committing_engine: AsyncEngine) -> None:
    metrics = await run_product_workload(
        committing_engine,
        concurrency=24,
        ops_per_worker=3,
        abandon_rate=0.25,
        dup_rate=0.25,
        seed=7,
    )
    assert metrics.violations == ()  # full oracle: conservation + exactly-once + balances
    assert metrics.overspend_micro == 0  # the real guard + storage CHECK never overspend
    assert metrics.committed > 0  # work actually happened
    assert metrics.reaped > 0  # abandoned reserves were reclaimed exactly once
    assert metrics.duplicates > 0  # the duplicate-idempotency-key dedup path executed
