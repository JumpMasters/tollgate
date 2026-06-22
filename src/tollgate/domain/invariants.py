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
