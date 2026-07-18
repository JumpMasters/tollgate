"""PostgresReservationRepository: reservation persistence + the identity guards (§5.2, §5.4).

``insert`` writes a held reservation and its per-node lines in the active command transaction.
``claim_terminal`` is the identity guard: an ``UPDATE … WHERE status = 'held'`` that moves the
reservation to exactly one terminal state — the conditional ``WHERE`` is what makes a terminal
effect exactly-once; ``claim_late_commit`` is the same mechanism for the one legal post-reap
transition (``reaped → committed``, the §5.4 self-heal, ADR 0029). ``find`` / ``find_lines``
read a reservation back for the terminal commands (the line view joins ``budget`` so callers
can walk balances in the canonical lock order), and ``advance_ttl`` is the monotonic heartbeat.
The multi-budget orchestration that decides *when* to call these lives a layer up (plans
07/09-10).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import budget, reservation_line
from tollgate.adapters.postgres.schema import reservation as reservation_table
from tollgate.domain.ids import BudgetId, PrincipalId, ReservationId
from tollgate.domain.records import (
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ScopeKind


class PostgresReservationRepository:
    """Reservation rows, their lines, and the held→terminal claim on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    @staticmethod
    def _record_from_row(row: Any) -> ReservationRecord:
        """Build the immutable reservation record from a full reservation row."""
        return ReservationRecord(
            reservation_id=ReservationId(row.reservation_id),
            idempotency_key=row.idempotency_key,
            principal_id=PrincipalId(row.principal_id),
            provider=row.provider,
            model=row.model,
            price_book_version=row.price_book_version,
            estimated_micro=row.estimated_micro,
            input_bound_tokens=row.input_bound_tokens,
            max_output_tokens=row.max_output_tokens,
            ttl_deadline=row.ttl_deadline,
            labels=row.labels,
        )

    async def insert(
        self,
        reservation: ReservationRecord,
        lines: Sequence[ReservationLineRecord],
    ) -> None:
        """Persist the held reservation and its lines (status/created_at DB-defaulted)."""
        await self._conn.execute(
            insert(reservation_table).values(
                reservation_id=reservation.reservation_id,
                idempotency_key=reservation.idempotency_key,
                principal_id=reservation.principal_id,
                provider=reservation.provider,
                model=reservation.model,
                price_book_version=reservation.price_book_version,
                estimated_micro=reservation.estimated_micro,
                input_bound_tokens=reservation.input_bound_tokens,
                max_output_tokens=reservation.max_output_tokens,
                ttl_deadline=reservation.ttl_deadline,
                labels=dict(reservation.labels),
            )
        )
        # A reservation always has >=1 line (a request governed by no budget is
        # denied, plan 07); the guard only skips an empty executemany.
        if lines:
            await self._conn.execute(
                insert(reservation_line),
                [
                    {
                        "reservation_id": line.reservation_id,
                        "budget_id": line.budget_id,
                        "period_start": line.period_start,
                        "amount_micro": line.amount_micro,
                    }
                    for line in lines
                ],
            )

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        """Move a held reservation to ``next_status``; return whether this caller won (§5.2)."""
        stmt = (
            update(reservation_table)
            .where(
                reservation_table.c.reservation_id == reservation_id,
                reservation_table.c.status == ReservationStatus.HELD,
            )
            .values(status=next_status)
        )
        result = await self._conn.execute(stmt)
        return result.rowcount == 1

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        """Return the reservation row and its live status, or ``None`` if the id is unknown."""
        row = (
            await self._conn.execute(
                select(reservation_table).where(
                    reservation_table.c.reservation_id == reservation_id
                )
            )
        ).first()
        if row is None:
            return None
        record = self._record_from_row(row)
        return StoredReservation(record=record, status=ReservationStatus(row.status))

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        """Return the reservation's lines joined with their budget nodes (§5.3, §5.4).

        The join to ``budget`` recovers each line's scope, so callers can walk the balances in
        the canonical lock order; the line's own ``period_start`` is carried because a late
        commit replays against each line's original period (ADR 0029).
        """
        rows = (
            await self._conn.execute(
                select(
                    reservation_line.c.budget_id,
                    reservation_line.c.period_start,
                    reservation_line.c.amount_micro,
                    budget.c.scope_kind,
                    budget.c.scope_id,
                )
                .select_from(
                    reservation_line.join(
                        budget, reservation_line.c.budget_id == budget.c.budget_id
                    )
                )
                .where(reservation_line.c.reservation_id == reservation_id)
            )
        ).all()
        return [
            ReservationLineView(
                node=BudgetNode(BudgetId(row.budget_id), ScopeKind(row.scope_kind), row.scope_id),
                period_start=row.period_start,
                amount_micro=row.amount_micro,
            )
            for row in rows
        ]

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        """Move a reaped reservation to committed; return whether this caller won (ADR 0029).

        The §5.4 self-heal guard: the one legal post-reap transition, claimed by the same
        conditional-``WHERE`` mechanism as :meth:`claim_terminal`, so exactly one late commit
        records the spend.
        """
        stmt = (
            update(reservation_table)
            .where(
                reservation_table.c.reservation_id == reservation_id,
                reservation_table.c.status == ReservationStatus.REAPED,
            )
            .values(status=ReservationStatus.COMMITTED)
        )
        result = await self._conn.execute(stmt)
        return result.rowcount == 1

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        """Monotonically advance a held reservation's TTL; return the resulting deadline (§5.4).

        ``GREATEST`` keeps the stored deadline from ever moving backward, so a stale heartbeat
        (an older replica's clock, a delayed retry) can never shorten a newer one — which is
        what makes extend naturally idempotent. Returns ``None`` when the reservation is not
        held (unknown, terminal, or reaped): there is nothing left to keep alive.
        """
        stmt = (
            update(reservation_table)
            .where(
                reservation_table.c.reservation_id == reservation_id,
                reservation_table.c.status == ReservationStatus.HELD,
            )
            .values(ttl_deadline=func.greatest(reservation_table.c.ttl_deadline, ttl_deadline))
            .returning(reservation_table.c.ttl_deadline)
        )
        row = (await self._conn.execute(stmt)).first()
        return None if row is None else row.ttl_deadline

    async def claim_next_expired(self, now: datetime) -> StoredReservation | None:
        """Claim and reap the oldest expired held reservation, or ``None`` if none remain (§5.4).

        The canonical Postgres queue claim: a ``FOR UPDATE SKIP LOCKED`` sub-select picks one
        held reservation past its ``ttl_deadline`` (skipping rows another reaper or a racing
        commit already locks), and the enclosing ``UPDATE`` flips it to ``reaped`` and returns the
        row. Because ``extend`` advances ``ttl_deadline`` monotonically, ``ttl_deadline < now``
        already means "not heartbeated recently" — no separate heartbeat column is needed. The
        caller releases this reservation's held estimate on its lines in the same transaction, so
        the status flip and the balance release commit atomically (exactly-once, §5.2).
        """
        picked = (
            select(reservation_table.c.reservation_id)
            .where(
                reservation_table.c.status == ReservationStatus.HELD,
                reservation_table.c.ttl_deadline < now,
            )
            .order_by(reservation_table.c.ttl_deadline)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(reservation_table)
            .where(reservation_table.c.reservation_id == picked)
            .values(status=ReservationStatus.REAPED)
            .returning(reservation_table)
        )
        row = (await self._conn.execute(stmt)).first()
        if row is None:
            return None
        return StoredReservation(record=self._record_from_row(row), status=ReservationStatus.REAPED)
