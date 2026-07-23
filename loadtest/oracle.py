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
    return OracleReport(violations=tuple(violations))
