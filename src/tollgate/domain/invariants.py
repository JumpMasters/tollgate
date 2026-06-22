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
