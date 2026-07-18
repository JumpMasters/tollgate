"""Read-side value types and pure alert/utilization logic for the chargeback API (section 2, 5.0).

A ``BudgetState`` is one budget node's balance for the current period plus its configured
soft-alert thresholds; a chargeback read returns the set of states at or below a credential's
scope (section 5.0). Utilization is reserved-inclusive -- ``(reserved + committed + overage) /
limit`` -- so it matches the ``remaining`` headroom the reserve guard actually gates on (section
3): an alert says how much room to reserve is gone, not merely how much has settled. Pure: no
I/O, no application or adapter imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tollgate.domain.ids import BudgetId
from tollgate.domain.invariants import Balance, remaining
from tollgate.domain.scopes import ScopeKind


@dataclass(frozen=True, slots=True)
class BudgetState:
    """One budget node's current-period balance and its soft-alert thresholds (section 2, 3)."""

    budget_id: BudgetId
    scope_kind: ScopeKind
    scope_id: str
    balance: Balance
    alert_thresholds_pct: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BudgetStatesView:
    """The result of a chargeback read: the in-scope states, all for one ``period_start``.

    Section 2.
    """

    period_start: datetime
    states: tuple[BudgetState, ...]


def remaining_micro(state: BudgetState) -> int:
    """Spendable headroom ``limit - reserved - committed - overage``; may be negative.

    Section 3.
    """
    return remaining(state.balance)


def spent_micro(state: BudgetState) -> int:
    """Reserved-inclusive consumption ``reserved + committed + overage`` -- the alert basis.

    Section 3.
    """
    return state.balance.limit_micro - remaining(state.balance)


def utilization_pct(state: BudgetState) -> int:
    """Reserved-inclusive utilization as a floored whole percent; 0 when the limit is non-positive.

    May exceed 100 once audited overage drives spend past the limit (section 3), reported faithfully
    rather than clamped.
    """
    limit = state.balance.limit_micro
    if limit <= 0:
        return 0
    return spent_micro(state) * 100 // limit


def crossed_thresholds(state: BudgetState) -> tuple[int, ...]:
    """The configured thresholds the node has reached, ascending (section 3; never blocks).

    A threshold ``t`` is crossed iff ``spent / limit >= t / 100``, tested as ``spent * 100 >= t *
    limit`` to stay in exact integers. A non-positive limit crosses nothing.
    """
    limit = state.balance.limit_micro
    if limit <= 0:
        return ()
    spent = spent_micro(state)
    return tuple(t for t in sorted(state.alert_thresholds_pct) if spent * 100 >= t * limit)


class GroupByKind(StrEnum):
    """The dimension a spend rollup groups by (section 2)."""

    PROVIDER = "provider"
    MODEL = "model"
    LABEL = "label"


@dataclass(frozen=True, slots=True)
class GroupBy:
    """A parsed group-by dimension: ``provider``, ``model``, or a ``label:<key>`` (section 2)."""

    kind: GroupByKind
    label_key: str | None = None


def parse_group_by(raw: str) -> GroupBy | None:
    """Parse a ``group_by`` token into a :class:`GroupBy`, or ``None`` if malformed.

    ``provider`` and ``model`` are first-class ledger/reservation dimensions; ``label:<key>``
    groups by an arbitrary key in the reservation's labels (so ``env`` and ``cost-center`` are just
    ``label:env`` / ``label:cost-center``). A non-empty key after ``label:`` is required.
    """
    if raw in (GroupByKind.PROVIDER.value, GroupByKind.MODEL.value):
        return GroupBy(kind=GroupByKind(raw))
    prefix, sep, key = raw.partition(":")
    if prefix == GroupByKind.LABEL.value and sep and key:
        return GroupBy(kind=GroupByKind.LABEL, label_key=key)
    return None


@dataclass(frozen=True, slots=True)
class SpendGroup:
    """One group of a spend rollup: the group's value and its realized micro-USD spend (section 2).

    ``group`` is ``None`` for spend that cannot be attributed on the requested dimension -- a
    grace-backfill row (no reservation) or a reservation missing the requested label key.
    """

    group: str | None
    spend_micro: int


@dataclass(frozen=True, slots=True)
class SpendRollup:
    """A scope node's realized spend for one period, grouped by a dimension (section 2)."""

    period_start: datetime
    groups: tuple[SpendGroup, ...]
