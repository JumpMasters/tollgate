"""The reaper handlers: release abandoned reservations and delete aged idempotency keys (§5.4-5.5).

Reaping is a system-driven cancel. ``ReservationReaperHandler.run_once`` polls
``claim_next_expired`` (a ``FOR UPDATE SKIP LOCKED`` claim-and-reap), and for each reaped
reservation releases its held estimate on every line in the canonical §5.3 lock order and appends
one ``reap`` ledger row per node — each in its own bounded transaction. The status flip is the
exactly-once guard, so a mainline commit that raced the reap routes to the self-healing late
commit (ADR 0029). ``IdempotencyReaperHandler.run_once`` batch-deletes keys past their TTL. The
ticks are pure of scheduling — the polling loop lives in ``workers/runner.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from tollgate.application.handlers.common import ordered_lines
from tollgate.application.ports import Clock, IdGenerator, UnitOfWork
from tollgate.domain.records import LedgerEntry, LedgerKind


@dataclass(frozen=True, slots=True)
class ReapReport:
    """The outcome of one reservation-reaper tick: how many held reservations were reaped."""

    reaped: int


class ReservationReaperHandler:
    """Releases held reservations past their TTL, one bounded transaction each (§5.4, §5.5)."""

    def __init__(self, *, uow: UnitOfWork, clock: Clock, ids: IdGenerator, batch_size: int) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._batch_size = batch_size

    async def run_once(self) -> ReapReport:
        """Reap up to ``batch_size`` expired reservations; each in its own SKIP LOCKED tx.

        A fixed ``now`` (captured once) keeps the tick deterministic: a reservation that expires
        mid-tick is caught on the next one. The per-item transaction claims-and-reaps one
        reservation, releases every line, and appends the ``reap`` rows, committing them
        atomically.
        """
        now = self._clock.now()
        reaped = 0
        while reaped < self._batch_size:
            async with self._uow.begin() as tx:
                stored = await tx.reservations.claim_next_expired(now)
                if stored is None:
                    break
                reservation_id = stored.record.reservation_id
                lines = ordered_lines(await tx.reservations.find_lines(reservation_id))
                entries: list[LedgerEntry] = []
                for line in lines:
                    await tx.counter_store.release(
                        line.node.budget_id, line.period_start, line.amount_micro
                    )
                    entries.append(
                        LedgerEntry(
                            entry_id=self._ids.new_ledger_entry_id(),
                            kind=LedgerKind.REAP,
                            budget_id=line.node.budget_id,
                            period_start=line.period_start,
                            reservation_id=reservation_id,
                            delta_reserved_micro=-line.amount_micro,
                            provider=stored.record.provider,
                            price_book_version=stored.record.price_book_version,
                        )
                    )
                await tx.ledger.append(entries)
            reaped += 1
        return ReapReport(reaped=reaped)
