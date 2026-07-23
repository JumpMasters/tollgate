"""Comparative load harness (§7): a concurrent reserve workload on a hot shared parent budget,
run against three admission-control strategies and audited by the offline oracle.

Part A — the shootout — runs three reserve strategies (naive read-then-reserve, value-CAS
optimistic concurrency, and the invariant-guarded conditional write the product uses) against a
dedicated ``harness_balance`` table with NO storage CHECK, so a guard failure shows up as real,
countable overspend rather than a constraint error, and reports throughput / p99 / overspend /
CAS retries — the "bug → fix → proof" numbers table. Part B — the product-path proof — drives the
real reserve/commit/cancel handlers plus the reaper on the real schema at high concurrency and runs
the full oracle. asyncio-only: N committing connections race on the shared parent row (real row
contention, independent of the GIL). Deterministically seeded; the CLI
(``python -m loadtest.harness``) runs the on-demand sweep. This is a demonstration tool, not
shipped runtime — the naive/OCC
strategies are deliberately-flawed strawmen; only the guarded write is the product's own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tollgate.domain.invariants import Balance

NodeKey = tuple[str, datetime]


@dataclass(frozen=True, slots=True)
class HarnessTree:
    """A hot shared parent plus its contending children, for the shootout workload."""

    parent_id: str
    parent_limit: int
    child_ids: tuple[str, ...]
    child_limit: int
    period: datetime


@dataclass(frozen=True, slots=True)
class ReserveOutcome:
    """Whether a strategy admitted the reserve, and how many CAS retries it burned (OCC)."""

    admitted: bool
    retries: int = 0


# ---- the CHECK-less demonstration table (parallel to budget_balance, minus the storage guard) ----

_HARNESS_BALANCE_DDL = """
CREATE TABLE IF NOT EXISTS harness_balance (
    budget_id text NOT NULL,
    period_start timestamptz NOT NULL,
    limit_micro bigint NOT NULL,
    reserved_micro bigint NOT NULL DEFAULT 0,
    committed_micro bigint NOT NULL DEFAULT 0,
    overage_micro bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (budget_id, period_start)
)
"""


async def _ensure_harness_balance(conn: AsyncConnection) -> None:
    """Create the demonstration table if absent and clear any rows from a prior run."""
    await conn.execute(text(_HARNESS_BALANCE_DDL))
    await conn.execute(text("TRUNCATE harness_balance"))


async def _seed_harness_balance(conn: AsyncConnection, tree: HarnessTree) -> None:
    """Seed the hot parent and its children at zero usage for ``tree.period``."""
    await conn.execute(
        text(
            "INSERT INTO harness_balance (budget_id, period_start, limit_micro) "
            "VALUES (:b, :p, :lim)"
        ),
        {"b": tree.parent_id, "p": tree.period, "lim": tree.parent_limit},
    )
    for child_id in tree.child_ids:
        await conn.execute(
            text(
                "INSERT INTO harness_balance (budget_id, period_start, limit_micro) "
                "VALUES (:b, :p, :lim)"
            ),
            {"b": child_id, "p": tree.period, "lim": tree.child_limit},
        )


async def _read_harness_balances(conn: AsyncConnection) -> dict[NodeKey, Balance]:
    """Load every ``harness_balance`` row as an oracle ``Balance`` keyed by (budget_id, period)."""
    result = await conn.execute(
        text(
            "SELECT budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
            "overage_micro FROM harness_balance"
        )
    )
    balances: dict[NodeKey, Balance] = {}
    for row in result:
        balances[(str(row.budget_id), row.period_start)] = Balance(
            limit_micro=int(row.limit_micro),
            reserved_micro=int(row.reserved_micro),
            committed_micro=int(row.committed_micro),
            overage_micro=int(row.overage_micro),
        )
    return balances


async def _drop_harness_balance(engine: AsyncEngine) -> None:
    """Remove the demonstration table (it is not part of the product schema)."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS harness_balance"))


def _overspend_micro(balances: dict[NodeKey, Balance]) -> int:
    """Total micro-USD admitted past every node's limit (``reserved+committed+overage - limit``)."""
    return sum(
        max(b.reserved_micro + b.committed_micro + b.overage_micro - b.limit_micro, 0)
        for b in balances.values()
    )


# ---- the three admission-control strategies (they differ ONLY in the reserve guard) ----


class ReserveStrategy(Protocol):
    """A reserve admission policy over ``harness_balance``: all-or-nothing across ``node_ids``."""

    name: str

    async def reserve(
        self, conn: AsyncConnection, node_ids: Sequence[str], period: datetime, amount: int
    ) -> ReserveOutcome: ...


_SELECT_REMAINING = text(
    "SELECT limit_micro - reserved_micro - committed_micro - overage_micro AS remaining "
    "FROM harness_balance WHERE budget_id = :b AND period_start = :p"
)


class GuardedStrategy:
    """The product's invariant-guarded conditional write: the ``WHERE`` is the admission guard.

    Mirrors ``PostgresCounterStore.reserve`` — one atomic ``UPDATE … WHERE remaining >= :amt`` per
    node in deterministic order; a zero-row update means the node is short → deny (all-or-nothing).
    """

    name = "guarded"

    async def reserve(
        self, conn: AsyncConnection, node_ids: Sequence[str], period: datetime, amount: int
    ) -> ReserveOutcome:
        for budget_id in sorted(node_ids):
            result = await conn.execute(
                text(
                    "UPDATE harness_balance SET reserved_micro = reserved_micro + :amt "
                    "WHERE budget_id = :b AND period_start = :p AND "
                    "limit_micro - reserved_micro - committed_micro - overage_micro >= :amt"
                ),
                {"amt": amount, "b": budget_id, "p": period},
            )
            if result.rowcount != 1:
                return ReserveOutcome(admitted=False)
        return ReserveOutcome(admitted=True)


class NaiveStrategy:
    """Read-then-reserve with a check-then-act gap: SELECT remaining, then unconditionally add.

    The unconditional ``UPDATE`` never loses updates (Postgres serialises ``reserved + :amt``), but
    the admission DECISION is made on a stale read, so concurrent workers all pass a check they
    should not and over-admit the shared parent. The strawman the guard exists to beat.
    """

    name = "naive"

    async def reserve(
        self, conn: AsyncConnection, node_ids: Sequence[str], period: datetime, amount: int
    ) -> ReserveOutcome:
        for budget_id in sorted(node_ids):
            remaining = int(
                (await conn.execute(_SELECT_REMAINING, {"b": budget_id, "p": period})).scalar_one()
            )
            if remaining < amount:
                return ReserveOutcome(admitted=False)
            await conn.execute(
                text(
                    "UPDATE harness_balance SET reserved_micro = reserved_micro + :amt "
                    "WHERE budget_id = :b AND period_start = :p"
                ),
                {"amt": amount, "b": budget_id, "p": period},
            )
        return ReserveOutcome(admitted=True)


class OccStrategy:
    """Optimistic concurrency: read the balance, then value-compare-and-swap; retry on a lost race.

    No version column (the product deliberately has none) — the CAS pins the three balance values
    it read. A concurrent commit changes them, the ``WHERE`` matches zero rows, and OCC re-reads and
    retries. Correct, but the retries THRASH the hot parent row — the "correct but slow" middle.
    """

    name = "occ"

    def __init__(self, max_retries: int = 200) -> None:
        self._max_retries = max_retries

    async def reserve(
        self, conn: AsyncConnection, node_ids: Sequence[str], period: datetime, amount: int
    ) -> ReserveOutcome:
        retries = 0
        for budget_id in sorted(node_ids):
            while True:
                row = (
                    await conn.execute(
                        text(
                            "SELECT limit_micro, reserved_micro, committed_micro, overage_micro "
                            "FROM harness_balance WHERE budget_id = :b AND period_start = :p"
                        ),
                        {"b": budget_id, "p": period},
                    )
                ).one()
                remaining = (
                    int(row.limit_micro)
                    - int(row.reserved_micro)
                    - int(row.committed_micro)
                    - int(row.overage_micro)
                )
                if remaining < amount:
                    return ReserveOutcome(admitted=False, retries=retries)
                result = await conn.execute(
                    text(
                        "UPDATE harness_balance SET reserved_micro = reserved_micro + :amt "
                        "WHERE budget_id = :b AND period_start = :p AND reserved_micro = :r "
                        "AND committed_micro = :c AND overage_micro = :o"
                    ),
                    {
                        "amt": amount,
                        "b": budget_id,
                        "p": period,
                        "r": int(row.reserved_micro),
                        "c": int(row.committed_micro),
                        "o": int(row.overage_micro),
                    },
                )
                if result.rowcount == 1:
                    break
                retries += 1
                if retries >= self._max_retries:  # livelock guard: give up rather than spin forever
                    return ReserveOutcome(admitted=False, retries=retries)
        return ReserveOutcome(admitted=True, retries=retries)


STRATEGIES: dict[str, ReserveStrategy] = {
    "naive": NaiveStrategy(),
    "occ": OccStrategy(),
    "guarded": GuardedStrategy(),
}
