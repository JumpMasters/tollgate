"""The extend command handler: the reservation heartbeat (§4, §5.4).

A long-running or streaming call advances its reservation's ``ttl_deadline`` so the reaper
never treats a live call as abandoned. Extend is **monotonic** — the adapter's ``GREATEST``
keeps the stored deadline from ever moving backward — which makes it naturally idempotent, so
it carries no idempotency key and skips that step of the §5 envelope; it still runs inside the
``UnitOfWork`` so the ownership check and the advance commit atomically. It touches no
balances and appends no ledger rows: a heartbeat is liveness, not spend.
"""

from __future__ import annotations

from datetime import timedelta

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import load_owned_reservation
from tollgate.application.ports import Clock, UnitOfWork
from tollgate.domain.commands import ExtendCommand, ExtendResult
from tollgate.domain.errors import ReservationNotHeld


class ExtendHandler:
    """Runs the extend heartbeat inside one transaction (§5.4)."""

    def __init__(self, *, uow: UnitOfWork, clock: Clock, reservation_ttl_seconds: int) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl_seconds = reservation_ttl_seconds

    async def extend(self, auth: AuthContext, command: ExtendCommand) -> ExtendResult:
        """Advance the reservation's TTL to now + the configured window (§5.4).

        Returns the deadline actually stored — never earlier than what was already there
        (monotonic). Raises :class:`ScopeNotAuthorized` for an unknown or foreign reservation
        (identically — no existence leak) and :class:`ReservationNotHeld` when the reservation
        is no longer held: there is nothing left to keep alive, and the caller should stop
        heartbeating.
        """
        async with self._uow.begin() as tx:
            await load_owned_reservation(tx.reservations, auth, command.reservation_id)
            deadline = self._clock.now() + timedelta(seconds=self._ttl_seconds)
            advanced = await tx.reservations.advance_ttl(command.reservation_id, deadline)
            if advanced is None:
                raise ReservationNotHeld
            return ExtendResult(reservation_id=command.reservation_id, ttl_deadline=advanced)
