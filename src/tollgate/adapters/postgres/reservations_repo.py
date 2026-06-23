"""PostgresReservationRepository: reservation persistence + the identity guard (§5.2).

``insert`` writes a held reservation and its per-node lines in the active command transaction.
``claim_terminal`` is the identity guard: an ``UPDATE … WHERE status = 'held'`` that moves the
reservation to exactly one terminal state — the conditional ``WHERE`` is what makes a terminal
effect exactly-once. A second claim matches zero rows (status is no longer ``held``) and
returns ``False`` → idempotent replay / self-heal (§5.4). The multi-budget orchestration that
decides *when* to call these lives a layer up (plans 07/09-10).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import reservation as reservation_table
from tollgate.adapters.postgres.schema import reservation_line
from tollgate.domain.ids import ReservationId
from tollgate.domain.records import ReservationLineRecord, ReservationRecord
from tollgate.domain.reservations import ReservationStatus


class PostgresReservationRepository:
    """Reservation rows, their lines, and the held→terminal claim on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

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
