"""Offline invariant + conservation oracle over a finished ledger (ADR 0011).

The command path never sums the ledger; this module audits a *finished* run. For every
``(budget, period)`` it reconstructs the balance from the append-only ledger and checks the
per-node spend invariants, conservation, exactly-once terminal effects, and — on the normal
enforcement path — parent/child roll-up. It reuses the pure predicates in
``tollgate.domain.invariants`` so it checks the *same* definitions the stateful machine asserts
step by step. A pure core (:func:`evaluate` over plain rows) does the checking; a thin async
reader (:func:`load_and_check`) loads the rows from Postgres. The comparative load harness,
built later, runs it over a high-concurrency run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import (
    budget,
    budget_balance,
    ledger,
    reservation,
    team,
    user_principal,
)
from tollgate.domain.invariants import (
    Balance,
    LedgerDelta,
    amounts_non_negative,
    committed_rolls_up,
    committed_within_limit,
    conserves,
    reservation_within_limit,
)

NodeKey = tuple[str, datetime]


class Check(StrEnum):
    """The offline checks the oracle can run; a run may select a subset."""

    NON_NEGATIVE = "non_negative"
    NO_BREACH = "no_breach"
    STORAGE_GUARD = "storage_guard"
    CONSERVATION = "conservation"
    EXACTLY_ONCE = "exactly_once"
    TREE_ROLLUP = "tree_rollup"


ALL_CHECKS: frozenset[Check] = frozenset(Check)


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One append-only ledger row's signed effect on a ``(budget, period)``."""

    budget_id: str
    period_start: datetime
    reservation_id: str | None
    delta_reserved_micro: int
    delta_committed_micro: int
    delta_overage_micro: int


@dataclass(frozen=True, slots=True)
class ReservationRow:
    """A reservation's terminal-state marker for the exactly-once audit."""

    reservation_id: str
    status: str


@dataclass(frozen=True, slots=True)
class TreeEdge:
    """A budgeted parent → budgeted child edge on the enforcement path (roll-up)."""

    parent_budget_id: str
    child_budget_id: str


@dataclass(frozen=True, slots=True)
class Violation:
    """One failed check: which check, which node/reservation, and a human detail."""

    check: Check
    scope: str
    detail: str


@dataclass(frozen=True, slots=True)
class OracleReport:
    """The result of an oracle run: an empty ``violations`` means every check passed."""

    violations: tuple[Violation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def _node_scope(key: NodeKey) -> str:
    budget_id, period_start = key
    return f"budget={budget_id} period={period_start.isoformat()}"


def _per_node_deltas(rows: list[LedgerRow]) -> dict[NodeKey, list[LedgerDelta]]:
    deltas: dict[NodeKey, list[LedgerDelta]] = {}
    for row in rows:
        deltas.setdefault((row.budget_id, row.period_start), []).append(
            LedgerDelta(
                delta_reserved_micro=row.delta_reserved_micro,
                delta_committed_micro=row.delta_committed_micro,
                delta_overage_micro=row.delta_overage_micro,
            )
        )
    return deltas


def _exactly_once(rows: list[LedgerRow]) -> list[Violation]:
    """One reserve effect and at most one hold-release effect per (reservation, budget).

    Each reservation reserves a node once (one positive ``delta_reserved``) and releases that
    hold at most once (one negative ``delta_reserved`` — a commit-of-held, cancel, or reap). The
    self-healing late commit adds committed spend with ``delta_reserved == 0``, so it does not
    count as a second release. Grace/meter rows carry no ``reservation_id`` and are skipped.
    """
    positive: dict[tuple[str, str], int] = {}
    negative: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.reservation_id is None:
            continue
        key = (row.reservation_id, row.budget_id)
        if row.delta_reserved_micro > 0:
            positive[key] = positive.get(key, 0) + 1
        elif row.delta_reserved_micro < 0:
            negative[key] = negative.get(key, 0) + 1
    violations: list[Violation] = []
    for (reservation_id, budget_id), count in sorted(positive.items()):
        if count > 1:
            violations.append(
                Violation(
                    Check.EXACTLY_ONCE,
                    f"reservation={reservation_id} budget={budget_id}",
                    f"{count} reserve effects (expected 1)",
                )
            )
    for (reservation_id, budget_id), count in sorted(negative.items()):
        if count > 1:
            violations.append(
                Violation(
                    Check.EXACTLY_ONCE,
                    f"reservation={reservation_id} budget={budget_id}",
                    f"{count} hold-release effects (expected at most 1)",
                )
            )
    return violations


def _tree_rollup(
    balances: Mapping[NodeKey, Balance], tree_edges: list[TreeEdge]
) -> list[Violation]:
    """A budgeted parent's committed equals the sum of its budgeted children's, per period.

    Holds on the normal enforcement path; the self-healing late commit against divergent per-node
    remaining (ADR 0029) is a documented exception, so callers auditing a run that mixed
    self-heal omit this check.
    """
    committed: dict[str, dict[datetime, int]] = {}
    for (budget_id, period), balance in balances.items():
        committed.setdefault(budget_id, {})[period] = balance.committed_micro
    children: dict[str, list[str]] = {}
    for edge in tree_edges:
        children.setdefault(edge.parent_budget_id, []).append(edge.child_budget_id)
    violations: list[Violation] = []
    for parent, kids in sorted(children.items()):
        periods = set(committed.get(parent, {}))
        for kid in kids:
            periods |= set(committed.get(kid, {}))
        for period in sorted(periods):
            parent_committed = committed.get(parent, {}).get(period, 0)
            kid_committed = [committed.get(kid, {}).get(period, 0) for kid in kids]
            if not committed_rolls_up(parent_committed, kid_committed):
                violations.append(
                    Violation(
                        Check.TREE_ROLLUP,
                        f"budget={parent} period={period.isoformat()}",
                        f"parent {parent_committed} != sum children {sum(kid_committed)}",
                    )
                )
    return violations


def evaluate(
    *,
    balances: Mapping[NodeKey, Balance],
    ledger_rows: Iterable[LedgerRow],
    reservations: Iterable[ReservationRow],
    tree_edges: Iterable[TreeEdge],
    checks: frozenset[Check] = ALL_CHECKS,
) -> OracleReport:
    """Run the selected offline checks over a finished run's rows (ADR 0011).

    Pure: no I/O, no internal-package imports beyond the shared invariant predicates. Every
    argument is materialised once so a one-shot iterator is fine.
    """
    rows = list(ledger_rows)
    deltas = _per_node_deltas(rows)
    violations: list[Violation] = []
    for key, balance in balances.items():
        scope = _node_scope(key)
        if Check.NON_NEGATIVE in checks and not amounts_non_negative(balance):
            violations.append(Violation(Check.NON_NEGATIVE, scope, repr(balance)))
        if Check.NO_BREACH in checks and not committed_within_limit(balance):
            violations.append(
                Violation(
                    Check.NO_BREACH,
                    scope,
                    f"committed {balance.committed_micro} > limit {balance.limit_micro}",
                )
            )
        if Check.STORAGE_GUARD in checks and not reservation_within_limit(balance):
            violations.append(
                Violation(
                    Check.STORAGE_GUARD,
                    scope,
                    f"reserved+committed exceeds limit ({balance!r})",
                )
            )
        if Check.CONSERVATION in checks and not conserves(balance, deltas.get(key, [])):
            violations.append(
                Violation(Check.CONSERVATION, scope, f"ledger sums do not reconstruct {balance!r}")
            )
    rows_list = rows  # already materialised above
    if Check.EXACTLY_ONCE in checks:
        violations.extend(_exactly_once(rows_list))
    if Check.TREE_ROLLUP in checks:
        violations.extend(_tree_rollup(balances, list(tree_edges)))
    return OracleReport(violations=tuple(violations))


async def load_balances(conn: AsyncConnection) -> dict[NodeKey, Balance]:
    """Load every ``budget_balance`` row as an oracle ``Balance`` keyed by (budget_id, period)."""
    result = await conn.execute(
        select(
            budget_balance.c.budget_id,
            budget_balance.c.period_start,
            budget_balance.c.limit_micro,
            budget_balance.c.reserved_micro,
            budget_balance.c.committed_micro,
            budget_balance.c.overage_micro,
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


async def _load_ledger(conn: AsyncConnection) -> list[LedgerRow]:
    result = await conn.execute(
        select(
            ledger.c.budget_id,
            ledger.c.period_start,
            ledger.c.reservation_id,
            ledger.c.delta_reserved_micro,
            ledger.c.delta_committed_micro,
            ledger.c.delta_overage_micro,
        )
    )
    return [
        LedgerRow(
            budget_id=str(row.budget_id),
            period_start=row.period_start,
            reservation_id=None if row.reservation_id is None else str(row.reservation_id),
            delta_reserved_micro=int(row.delta_reserved_micro),
            delta_committed_micro=int(row.delta_committed_micro),
            delta_overage_micro=int(row.delta_overage_micro),
        )
        for row in result
    ]


async def _load_reservations(conn: AsyncConnection) -> list[ReservationRow]:
    result = await conn.execute(select(reservation.c.reservation_id, reservation.c.status))
    return [
        ReservationRow(reservation_id=str(row.reservation_id), status=str(row.status))
        for row in result
    ]


async def _load_tree_edges(conn: AsyncConnection) -> list[TreeEdge]:
    budgets = (
        await conn.execute(select(budget.c.budget_id, budget.c.scope_kind, budget.c.scope_id))
    ).all()
    team_org = {
        str(r.team_id): str(r.org_id)
        for r in (await conn.execute(select(team.c.team_id, team.c.org_id))).all()
    }
    user_team = {
        str(r.user_id): str(r.team_id)
        for r in (
            await conn.execute(select(user_principal.c.user_id, user_principal.c.team_id))
        ).all()
    }
    by_scope = {(str(r.scope_kind), str(r.scope_id)): str(r.budget_id) for r in budgets}
    edges: list[TreeEdge] = []
    for r in budgets:
        scope_kind, scope_id, budget_id = str(r.scope_kind), str(r.scope_id), str(r.budget_id)
        parent: str | None = None
        if scope_kind == "user":
            team_id = user_team.get(scope_id)
            parent = by_scope.get(("team", team_id)) if team_id is not None else None
        elif scope_kind == "team":
            org_id = team_org.get(scope_id)
            parent = by_scope.get(("org", org_id)) if org_id is not None else None
        if parent is not None:
            edges.append(TreeEdge(parent_budget_id=parent, child_budget_id=budget_id))
    return edges


async def load_and_check(
    conn: AsyncConnection, *, checks: frozenset[Check] = ALL_CHECKS
) -> OracleReport:
    """Load a finished run's rows from Postgres and run the selected checks (ADR 0011)."""
    return evaluate(
        balances=await load_balances(conn),
        ledger_rows=await _load_ledger(conn),
        reservations=await _load_reservations(conn),
        tree_edges=await _load_tree_edges(conn),
        checks=checks,
    )
