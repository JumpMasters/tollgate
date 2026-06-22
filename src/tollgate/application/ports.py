"""Ports: the interfaces the application depends on, expressed as Protocols.

Concrete adapters implement these. The application is written against the
protocols alone, so the Postgres store (and, later, a Redis fast-path) can be
swapped without touching handler logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tollgate.domain.ids import BudgetId, ReservationId


class CounterStore(Protocol):
    """The budget-balance primitives behind a reservation.

    Implementations enforce the spend invariant with guarded conditional writes:
    a reserve that would breach a limit must fail rather than overshoot.
    """

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        """Lazily create the period's balance row, seeded from the budget's limit.

        Idempotent (``INSERT … ON CONFLICT DO NOTHING``) so concurrent first-reservers
        in a new period converge on one row rather than failing (§5.3, §5.5).
        """
        ...

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        """Reserve ``amount_micro`` against a budget node; return whether it fit."""
        ...

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        """Move a reservation's estimate to committed, recording any overage."""
        ...

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        """Release a held reservation's estimate back to the node."""
        ...


class ReservationRepository(Protocol):
    """Persistence for reservation rows and their identity guard."""

    async def claim_terminal(self, reservation_id: ReservationId, next_status: str) -> bool:
        """Atomically move a reservation from held to a terminal state.

        Returns whether this caller won the claim, which is what makes a terminal
        effect exactly-once.
        """
        ...
