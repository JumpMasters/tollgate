"""Ports: the interfaces the application depends on, expressed as Protocols.

Concrete adapters implement these. The application is written against the protocols alone, so
the Postgres store (and, later, a Redis fast-path) can be swapped without touching handler
logic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from tollgate.domain.ids import BudgetId, ReservationId
from tollgate.domain.records import (
    IdempotencyClaim,
    LedgerEntry,
    ReservationLineRecord,
    ReservationRecord,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ReserveOutcome


class CounterStore(Protocol):
    """The budget-balance primitives behind a reservation.

    Implementations enforce the spend invariant with guarded conditional writes: a reserve
    that would breach a limit must fail rather than overshoot.
    """

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        """Lazily create the period's balance row, seeded from the budget's limit.

        Idempotent (``INSERT … ON CONFLICT DO NOTHING``) so concurrent first-reservers in a
        new period converge on one row rather than failing (§5.3, §5.5).
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
    """Persistence for reservation rows, their lines, and the identity guard."""

    async def insert(
        self,
        reservation: ReservationRecord,
        lines: Sequence[ReservationLineRecord],
    ) -> None:
        """Persist a held reservation and its per-node lines in the current transaction."""
        ...

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        """Atomically move a reservation from held to a terminal state.

        Returns whether this caller won the claim, which is what makes a terminal effect
        exactly-once (§5.2). A second claim for the same reservation finds ``status ≠ 'held'``,
        matches zero rows, and returns ``False`` → idempotent replay / self-heal (§5.4).
        """
        ...


class IdempotencyRepository(Protocol):
    """Claim/replay store for command idempotency keys (§5.1)."""

    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        """Claim ``key`` for ``fingerprint``.

        ``FRESH`` if newly inserted (the caller owns the effect); ``REPLAY`` with the stored
        response if the key already completed; ``MISMATCH`` if the key exists under a different
        command fingerprint (key reuse).
        """
        ...

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        """Cache a command's response on its key row so a later duplicate replays it."""
        ...


class LedgerRepository(Protocol):
    """Append-only writer for the audit ledger (§5.2)."""

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        """Append one or more ledger rows in the current transaction (never summed here)."""
        ...


class ReserveTransaction(Protocol):
    """Multi-budget, all-or-nothing guarded reserve across an applicable set (§5.2/§5.3)."""

    async def reserve(
        self,
        nodes: Sequence[BudgetNode],
        period_start: datetime,
        amount_micro: int,
    ) -> ReserveOutcome:
        """Reserve ``amount_micro`` on every applicable node in lock order, all-or-nothing.

        Returns ``ReserveOutcome(ok=True)`` iff every node had headroom. On the first node
        without headroom returns ``ReserveOutcome(ok=False, binding_node=node)`` and leaves the
        walk's earlier reserves in place for the caller's transaction to roll back (§5.3).
        """
        ...
