"""Pure spend-invariant predicates shared by the oracle and the stateful tests.

Section 7 defines the invariants the system must uphold at *every* budget node:
non-negativity, the no-breach guarantee (committed never exceeds limit), the storage
guard (reserved + committed <= limit), conservation against the append-only ledger,
and tree consistency across budgeted nodes. They are expressed here once, as pure
functions over plain balance values, so the Hypothesis stateful machine (asserting
after every step) and the offline conservation oracle (auditing a finished run) check
the *same* definitions. No I/O, no internal imports.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Balance:
    """A budget node's balance for one period (§3): the four micro-USD aggregates.

    ``remaining = limit - reserved - committed - overage``. Carries no identity --
    the oracle keys balances by ``(budget_id, period_start)`` externally.
    """

    limit_micro: int
    reserved_micro: int
    committed_micro: int
    overage_micro: int


def remaining(balance: Balance) -> int:
    """Spendable headroom: ``limit - reserved - committed - overage`` (§3).

    May be *negative* once audited overage has accrued -- overage counts against
    remaining without ever forcing ``committed`` past ``limit`` (§3), which is the
    signal that an overspent node stops admitting new reserves.
    """
    return (
        balance.limit_micro
        - balance.reserved_micro
        - balance.committed_micro
        - balance.overage_micro
    )


def can_reserve(balance: Balance, estimate_micro: int) -> bool:
    """Whether ``estimate_micro`` fits the node's headroom -- the §5.2 reserve guard.

    Mirrors the conditional UPDATE's ``WHERE limit - reserved - committed - overage
    >= :est``. Requires a non-negative estimate.
    """
    return estimate_micro >= 0 and remaining(balance) >= estimate_micro


def amounts_non_negative(balance: Balance) -> bool:
    """Every aggregate is non-negative -- the ``budget_balance`` CHECKs (§3)."""
    return (
        balance.limit_micro >= 0
        and balance.reserved_micro >= 0
        and balance.committed_micro >= 0
        and balance.overage_micro >= 0
    )


def committed_within_limit(balance: Balance) -> bool:
    """No breach: ``committed`` never exceeds ``limit`` -- the headline guarantee (§4)."""
    return balance.committed_micro <= balance.limit_micro


def reservation_within_limit(balance: Balance) -> bool:
    """Storage-tier guard: ``reserved + committed <= limit``, local to the row (§3, §5.2)."""
    return balance.reserved_micro + balance.committed_micro <= balance.limit_micro


def node_invariants_hold(balance: Balance) -> bool:
    """The per-node spend invariants, conjoined -- asserted after every step (§7).

    Non-negativity, no-breach (``committed <= limit``), and the storage guard
    (``reserved + committed <= limit``). It deliberately does *not* require
    ``remaining >= 0``: audited overage may drive ``remaining`` negative while every
    guarantee above still holds (§4).
    """
    return (
        amounts_non_negative(balance)
        and committed_within_limit(balance)
        and reservation_within_limit(balance)
    )


@dataclass(frozen=True, slots=True)
class LedgerDelta:
    """One ledger row's signed effect on a balance (§3 sign convention).

    Carries only the three deltas; the full ledger row (kind, ids, timestamps, token
    counts) is a persistence concern (plan 06). The oracle sums these per
    ``(budget, period)``.
    """

    delta_reserved_micro: int
    delta_committed_micro: int
    delta_overage_micro: int


def conserves(balance: Balance, deltas: Iterable[LedgerDelta]) -> bool:
    """Conservation (§3, §7): summed ledger deltas reconstruct the live balance.

    For one ``(budget, period)``: sum of delta_reserved == reserved, sum of
    delta_committed == committed, sum of delta_overage == overage. Catches the
    lost-update / double-apply bugs across the multi-budget reserve that the
    row-local CHECKs cannot. Single-pass, so a one-shot iterator is fine.
    """
    total_reserved = 0
    total_committed = 0
    total_overage = 0
    for delta in deltas:
        total_reserved += delta.delta_reserved_micro
        total_committed += delta.delta_committed_micro
        total_overage += delta.delta_overage_micro
    return (
        total_reserved == balance.reserved_micro
        and total_committed == balance.committed_micro
        and total_overage == balance.overage_micro
    )


def committed_rolls_up(parent_committed_micro: int, child_committed_micro: Iterable[int]) -> bool:
    """Tree consistency (§7): a budgeted parent's committed == sum of its children's.

    Applied per parent over its budgeted children on the enforcement path. The
    orthogonal ``project`` axis is summed independently and is *not* part of this
    relation.
    """
    return parent_committed_micro == sum(child_committed_micro)
