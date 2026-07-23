"""Offline invariant + conservation oracle over a finished ledger (§7, ADR 0011).

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
    """The offline checks the oracle can run; a run may select a subset (§7)."""

    NON_NEGATIVE = "non_negative"
    NO_BREACH = "no_breach"
    STORAGE_GUARD = "storage_guard"
    CONSERVATION = "conservation"
    EXACTLY_ONCE = "exactly_once"
    TREE_ROLLUP = "tree_rollup"


ALL_CHECKS: frozenset[Check] = frozenset(Check)


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One append-only ledger row's signed effect on a ``(budget, period)`` (§3)."""

    budget_id: str
    period_start: datetime
    reservation_id: str | None
    delta_reserved_micro: int
    delta_committed_micro: int
    delta_overage_micro: int


@dataclass(frozen=True, slots=True)
class ReservationRow:
    """A reservation's terminal-state marker for the exactly-once audit (§5.2)."""

    reservation_id: str
    status: str


@dataclass(frozen=True, slots=True)
class TreeEdge:
    """A budgeted parent → budgeted child edge on the enforcement path (§7 roll-up)."""

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
    """One reserve effect and at most one hold-release effect per (reservation, budget) (§5.2).

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
    """A budgeted parent's committed equals the sum of its budgeted children's, per period (§7).

    Holds on the normal enforcement path; the self-healing late commit against divergent per-node
    remaining (§5.4, ADR 0029) is a documented exception, so callers auditing a run that mixed
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
    """Run the selected offline checks over a finished run's rows (§7, ADR 0011).

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
