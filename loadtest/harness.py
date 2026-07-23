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

import argparse
import asyncio
import math
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from loadtest.oracle import ALL_CHECKS, Check, evaluate, load_and_check
from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.schema import (
    budget,
    budget_balance,
    org,
    price,
    price_book,
    team,
    user_principal,
)
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.auth import AuthContext
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.reap import ReservationReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import CommitCommand, ProviderUsage, ReserveCommand
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import InsufficientBudget, ReservationNotHeld
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.invariants import Balance
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.scopes import ScopeKind

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


_BALANCE_CHECKS: frozenset[Check] = frozenset(
    {Check.NON_NEGATIVE, Check.NO_BREACH, Check.STORAGE_GUARD}
)
_AMOUNTS = (50, 100, 150, 200)  # per-reserve demand, drawn from a seeded RNG


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """One strategy's shootout result: throughput, tail latency, and the overspend it admitted."""

    strategy: str
    concurrency: int
    ops: int
    admitted: int
    denied: int
    retries: int
    throughput_ops_per_s: float
    p99_ms: float
    overspend_micro: int
    violations: tuple[str, ...]


async def _worker(
    conn: AsyncConnection,
    strategy: ReserveStrategy,
    tree: HarnessTree,
    *,
    worker_id: int,
    ops_per_worker: int,
    seed: int,
    barrier: asyncio.Barrier,
) -> tuple[int, int, int, list[float]]:
    """Run one worker's seeded reserve sequence on a PRE-OPENED connection; return the tallies.

    All workers wait on ``barrier`` before their first op so their reads overlap in time. The naive
    check-then-act breach only surfaces when a read happens before other workers' writes commit; a
    synchronized start on already-open connections makes that reliable rather than timing-dependent
    (opening a fresh connection per op serialises the workers and hides the contention). Returns
    ``(admitted, denied, retries, latencies_s)``.
    """
    rng = random.Random((seed << 16) ^ worker_id)
    child = tree.child_ids[worker_id % len(tree.child_ids)]
    nodes = [tree.parent_id, child]
    admitted = denied = retries = 0
    latencies: list[float] = []
    for op in range(ops_per_worker):
        amount = rng.choice(_AMOUNTS)
        if op == 0:
            await barrier.wait()  # release all workers together so their first reads overlap
        started = time.perf_counter()
        txn = await conn.begin()
        try:
            outcome = await strategy.reserve(conn, nodes, tree.period, amount)
        except Exception:
            await txn.rollback()
            raise
        if outcome.admitted:
            await txn.commit()
        else:
            await txn.rollback()
        latencies.append(time.perf_counter() - started)
        retries += outcome.retries
        if outcome.admitted:
            admitted += 1
        else:
            denied += 1
    return admitted, denied, retries, latencies


def _p99_ms(latencies: Sequence[float]) -> float:
    """The 99th-percentile latency in milliseconds (nearest-rank; 0.0 for an empty sample)."""
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    index = max(0, math.ceil(len(ordered) * 0.99) - 1)  # nearest-rank; correct at multiples of 100
    return ordered[index] * 1000.0


async def run_strategy(
    engine: AsyncEngine,
    strategy: ReserveStrategy,
    *,
    tree: HarnessTree,
    concurrency: int,
    ops_per_worker: int,
    seed: int,
) -> RunMetrics:
    """Drive ``concurrency`` workers against ``strategy`` on the hot tree, then audit the result.

    Every worker commits its admitted reserves so the contention on the shared parent row is real.
    After the run the ``harness_balance`` rows are audited with the oracle's balance-tier checks and
    the admitted overspend is summed — zero for a correct guard, positive for the naive strawman.
    """
    async with engine.begin() as conn:
        await _ensure_harness_balance(conn)
        await _seed_harness_balance(conn, tree)
    # Pre-open one committing connection per worker OUTSIDE the timed section: connection-setup
    # latency would otherwise serialise the workers and hide the contention. A barrier then starts
    # them together so the naive over-admission is reliable, not timing-dependent.
    conns: list[AsyncConnection] = []
    barrier = asyncio.Barrier(concurrency)
    try:
        for _ in range(concurrency):
            conns.append(await engine.connect())
        started = time.perf_counter()
        results = await asyncio.gather(
            *(
                _worker(
                    conns[i],
                    strategy,
                    tree,
                    worker_id=i,
                    ops_per_worker=ops_per_worker,
                    seed=seed,
                    barrier=barrier,
                )
                for i in range(concurrency)
            )
        )
        elapsed = time.perf_counter() - started
    finally:
        for conn in conns:
            await conn.close()
    admitted = sum(r[0] for r in results)
    denied = sum(r[1] for r in results)
    retries = sum(r[2] for r in results)
    latencies = [lat for r in results for lat in r[3]]
    async with engine.connect() as conn:
        balances = await _read_harness_balances(conn)
    report = evaluate(
        balances=balances, ledger_rows=[], reservations=[], tree_edges=[], checks=_BALANCE_CHECKS
    )
    total_ops = concurrency * ops_per_worker
    return RunMetrics(
        strategy=strategy.name,
        concurrency=concurrency,
        ops=total_ops,
        admitted=admitted,
        denied=denied,
        retries=retries,
        throughput_ops_per_s=total_ops / elapsed if elapsed > 0 else 0.0,
        p99_ms=_p99_ms(latencies),
        overspend_micro=_overspend_micro(balances),
        violations=tuple(sorted({v.check.value for v in report.violations})),
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


_COLUMNS = (
    "strategy",
    "concurrency",
    "throughput/s",
    "p99_ms",
    "overspend",
    "retries",
    "violations",
)


def format_table(rows: Sequence[RunMetrics]) -> str:
    """Render the shootout results as a fixed-width table (header + one line per run)."""
    cells: list[tuple[str, ...]] = [_COLUMNS]
    for r in rows:
        cells.append(
            (
                r.strategy,
                str(r.concurrency),
                f"{r.throughput_ops_per_s:.0f}",
                f"{r.p99_ms:.2f}",
                str(r.overspend_micro),
                str(r.retries),
                ",".join(r.violations) or "-",
            )
        )
    widths = [max(len(row[i]) for row in cells) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in cells
    )


def _sweep_tree() -> HarnessTree:
    """The hot tree the sweep contends on: a small parent, many children (the shootout's shape)."""
    return HarnessTree(
        parent_id="p",
        parent_limit=1000,
        child_ids=tuple(f"c{i}" for i in range(8)),
        child_limit=100_000,
        period=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def run_sweep(
    engine: AsyncEngine, *, concurrencies: Sequence[int], ops_per_worker: int, seed: int
) -> list[RunMetrics]:
    """Run every strategy at every concurrency level, returning the metrics for the table."""
    rows: list[RunMetrics] = []
    tree = _sweep_tree()
    try:
        for concurrency in concurrencies:
            for strategy in STRATEGIES.values():
                rows.append(
                    await run_strategy(
                        engine,
                        strategy,
                        tree=tree,
                        concurrency=concurrency,
                        ops_per_worker=ops_per_worker,
                        seed=seed,
                    )
                )
    finally:
        await _drop_harness_balance(engine)
    return rows


def main(argv: Sequence[str] | None = None) -> None:
    """CLI for the on-demand sweep: print the numbers table for a running Postgres."""
    parser = argparse.ArgumentParser(description="Tollgate comparative reserve-guard load harness")
    parser.add_argument(
        "--concurrency",
        type=int,
        action="append",
        default=None,
        help="concurrency level(s); repeatable (default: 8 32 64)",
    )
    parser.add_argument("--ops", type=int, default=8, help="reserves per worker (default 8)")
    parser.add_argument("--seed", type=int, default=1, help="workload seed (default 1)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TOLLGATE_DATABASE_URL"),
        help="asyncpg URL (default $TOLLGATE_DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("no database URL: pass --database-url or set TOLLGATE_DATABASE_URL")
    concurrencies = args.concurrency or [8, 32, 64]

    async def _go() -> list[RunMetrics]:
        engine = create_async_engine(args.database_url, poolclass=pool.NullPool)
        try:
            return await run_sweep(
                engine, concurrencies=concurrencies, ops_per_worker=args.ops, seed=args.seed
            )
        finally:
            await engine.dispose()

    print(format_table(asyncio.run(_go())))


if __name__ == "__main__":
    main()


# ---- Part B: the product-path proof on the real schema (team budget = the hot shared parent) ----

_START = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = calendar_month_start(_START)
_TTL = 600
_PRODUCT_TABLES = (
    "org, team, user_principal, project, api_credential, price_book, price, budget, budget_alert, "
    "budget_balance, reservation, reservation_line, ledger, idempotency_key, metered_receipt"
)


class _MutableClock:
    """A clock the driver advances once, past the TTL, to reap the abandoned reservations."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: int) -> None:
        self._t += timedelta(seconds=seconds)


async def _seed_product_tree(engine: AsyncEngine, *, users: int, team_limit: int) -> None:
    """One org → one team (the hot shared parent) → ``users`` users, each with a generous budget."""
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_PRODUCT_TABLES} CASCADE"))
        await conn.execute(price_book.insert().values(version="pb-1", published_at=_PERIOD))
        await conn.execute(
            price.insert().values(
                price_book_version="pb-1",
                provider="anthropic",
                model="claude",
                input_micro_per_token=Decimal("1"),
                output_micro_per_token=Decimal("2"),
                cached_input_micro_per_token=Decimal("0.5"),
                cache_creation_micro_per_token=Decimal("1.25"),
            )
        )
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="T1"))
        await conn.execute(
            budget.insert().values(
                budget_id="b-org",
                scope_kind="org",
                scope_id="o1",
                period_kind="calendar_month",
                hard_limit_micro=1_000_000,
            )
        )
        await conn.execute(
            budget_balance.insert().values(
                budget_id="b-org", period_start=_PERIOD, limit_micro=1_000_000
            )
        )
        await conn.execute(
            budget.insert().values(
                budget_id="b-team",
                scope_kind="team",
                scope_id="t1",
                period_kind="calendar_month",
                hard_limit_micro=team_limit,
            )
        )
        await conn.execute(
            budget_balance.insert().values(
                budget_id="b-team", period_start=_PERIOD, limit_micro=team_limit
            )
        )
        for i in range(users):
            await conn.execute(
                user_principal.insert().values(user_id=f"u{i}", team_id="t1", external_ref=None)
            )
            await conn.execute(
                budget.insert().values(
                    budget_id=f"b-u{i}",
                    scope_kind="user",
                    scope_id=f"u{i}",
                    period_kind="calendar_month",
                    hard_limit_micro=1_000_000,
                )
            )
            await conn.execute(
                budget_balance.insert().values(
                    budget_id=f"b-u{i}", period_start=_PERIOD, limit_micro=1_000_000
                )
            )


def _auth_for(user: str) -> AuthContext:
    credential = Credential(
        credential_id=CredentialId(f"cred-{user}"),
        principal_id=PrincipalId(user),
        scope_kind=ScopeKind.USER,
        scope_id=user,
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(user_id=UserId(user), team_id=TeamId("t1"), org_id=OrgId("o1"))
    return AuthContext(credential=credential, principal=principal)


@dataclass(frozen=True, slots=True)
class ProductMetrics:
    """The product-path proof result: what happened, plus the full-oracle verdict over the
    ledger."""

    admitted: int
    committed: int
    abandoned: int
    reaped: int
    duplicates: int
    overspend_micro: int
    violations: tuple[str, ...]


async def run_product_workload(
    engine: AsyncEngine,
    *,
    concurrency: int,
    ops_per_worker: int,
    abandon_rate: float,
    dup_rate: float,
    seed: int,
) -> ProductMetrics:
    """Drive the real handlers on the hot team budget, then audit the ledger with the full oracle.

    Each worker is a user reserving against org→team→itself; the TEAM budget is the shared hot
    parent. A fraction of reserves are abandoned (never committed → reaped) and a fraction of
    commits replay under the same idempotency key (must dedup). After the run the clock advances
    past the TTL and the reaper reclaims the abandoned holds; the oracle then reconstructs the
    balances from the ledger and checks conservation + exactly-once (``TREE_ROLLUP`` excluded: the
    self-healing late commit legitimately diverges parent/child committed, §5.4/ADR 0029).
    """
    users = concurrency
    # team headroom well below total demand so the guard denies under contention (correctly)
    await _seed_product_tree(engine, users=users, team_limit=5000)
    clock = _MutableClock(_START)
    ids = Uuid7IdGenerator()
    reserve_h = ReserveHandler(
        uow=PostgresUnitOfWork(engine), clock=clock, ids=ids, reservation_ttl_seconds=_TTL
    )
    commit_h = CommitHandler(uow=PostgresUnitOfWork(engine), ids=ids)

    async def _worker(worker_id: int) -> tuple[int, int, int, int]:
        rng = random.Random((seed << 16) ^ worker_id)
        auth = _auth_for(f"u{worker_id}")
        admitted = committed = abandoned = duplicates = 0
        for op in range(ops_per_worker):
            key = f"idem-{worker_id}-{op}"
            command = ReserveCommand(
                idempotency_key=key,
                provider="anthropic",
                model="claude",
                input_bound_tokens=rng.choice((40, 80, 120)),
                max_output_tokens=rng.choice((0, 40)),
                labels={},
            )
            try:
                result = await reserve_h.reserve(auth, command)
            except InsufficientBudget:
                continue
            admitted += 1
            if rng.random() < abandon_rate:
                abandoned += 1
                continue  # leave it held → the reaper reclaims it
            usage = ProviderUsage(input_tokens=command.input_bound_tokens, output_tokens=0)
            commit_cmd = CommitCommand(
                idempotency_key=f"c-{key}", reservation_id=result.reservation_id, usage=usage
            )
            await commit_h.commit(auth, commit_cmd)
            committed += 1
            if rng.random() < dup_rate:  # replay the exact commit → must dedup, not double-count
                try:
                    await commit_h.commit(auth, commit_cmd)
                    duplicates += 1
                except ReservationNotHeld:  # pragma: no cover - replay returns the cached response
                    pass
        return admitted, committed, abandoned, duplicates

    results = await asyncio.gather(*(_worker(i) for i in range(concurrency)))
    admitted = sum(r[0] for r in results)
    committed = sum(r[1] for r in results)
    abandoned = sum(r[2] for r in results)
    duplicates = sum(r[3] for r in results)

    clock.advance(_TTL + 1)
    reaper = ReservationReaperHandler(
        uow=PostgresUnitOfWork(engine),
        clock=clock,
        ids=ids,
        batch_size=concurrency * ops_per_worker + 8,
    )
    report = await reaper.run_once()
    reaped = report.reaped

    async with engine.connect() as conn:
        oracle = await load_and_check(conn, checks=ALL_CHECKS - {Check.TREE_ROLLUP})
        balances = await _read_product_balances(conn)
    return ProductMetrics(
        admitted=admitted,
        committed=committed,
        abandoned=abandoned,
        reaped=reaped,
        duplicates=duplicates,
        overspend_micro=_overspend_micro(balances),
        violations=tuple(sorted({v.check.value for v in oracle.violations})),
    )


async def _read_product_balances(conn: AsyncConnection) -> dict[NodeKey, Balance]:
    """Load the real ``budget_balance`` rows as oracle ``Balance`` values (for the overspend
    sum)."""
    result = await conn.execute(
        text(
            "SELECT budget_id, period_start, limit_micro, reserved_micro, committed_micro, "
            "overage_micro FROM budget_balance"
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
