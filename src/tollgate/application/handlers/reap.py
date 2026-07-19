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

import logging
from dataclasses import dataclass
from datetime import timedelta

from tollgate.application.handlers.common import ordered_lines
from tollgate.application.ports import Clock, IdGenerator, UnitOfWork
from tollgate.domain.ids import ReservationId
from tollgate.domain.records import LedgerEntry, LedgerKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReapReport:
    """One reservation-reaper tick's outcome: reservations reaped, and per-item reap failures."""

    reaped: int
    failed: int = 0


class ReservationReaperHandler:
    """Releases held reservations past their TTL, one bounded transaction each (§5.4, §5.5)."""

    def __init__(self, *, uow: UnitOfWork, clock: Clock, ids: IdGenerator, batch_size: int) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._batch_size = batch_size

    async def run_once(self) -> ReapReport:
        """Reap expired reservations, each in its own SKIP LOCKED tx, isolating per-item failures.

        A fixed ``now`` (captured once) keeps the tick deterministic: a reservation that expires
        mid-tick is caught on the next one. Each per-item transaction claims-and-reaps one
        reservation, releases every line, and appends the ``reap`` rows, committing them
        atomically. A reap that raises is caught so one persistently failing reservation cannot
        abort the whole tick and starve everything behind it (#74): its id is excluded from the
        rest of this tick (its rolled-back status flip would otherwise put it back at the queue
        head) and the reaper moves to the next candidate. A failure in the *claim* itself (no id
        yet) is a datastore problem, not one poison row, so it propagates for the runner to handle.
        The tick is bounded to ``batch_size`` attempts; the next tick continues.
        """
        now = self._clock.now()
        reaped = 0
        failed = 0
        skip: list[ReservationId] = []
        while reaped + failed < self._batch_size:
            reservation_id: ReservationId | None = None
            try:
                async with self._uow.begin() as tx:
                    stored = await tx.reservations.claim_next_expired(now, skip)
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
            except Exception:
                if reservation_id is None:
                    raise  # the claim failed, not a single reap; let the runner handle it
                logger.exception(
                    "reservation reap failed for %s; skipping it this tick", reservation_id
                )
                skip.append(reservation_id)
                failed += 1
        return ReapReport(reaped=reaped, failed=failed)


class IdempotencyReaperHandler:
    """Batch-deletes idempotency keys past their TTL, one bounded transaction each (§5.5)."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ttl_hours: int,
        batch_size: int,
        max_batches_per_tick: int,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl_hours = ttl_hours
        self._batch_size = batch_size
        self._max_batches_per_tick = max_batches_per_tick

    async def run_once(self) -> int:
        """Delete keys older than ``ttl_hours`` in bounded batches; return the count removed.

        ``cutoff`` is fixed at tick start and ``batch_size >= 1``, so each full batch strictly
        shrinks the remainder and the drain stops as soon as a batch comes back short. The tick is
        also capped at ``max_batches_per_tick``: against a large backlog (first deploy against an
        aged table, or recovery after downtime) an uncapped drain could run for hours and starve
        the runner's between-tick stop check, defeating graceful shutdown — the cap keeps every
        tick bounded and lets the next tick continue the drain (#73).
        """
        cutoff = self._clock.now() - timedelta(hours=self._ttl_hours)
        deleted = 0
        for _batch in range(self._max_batches_per_tick):
            async with self._uow.begin() as tx:
                removed = await tx.idempotency.delete_expired(cutoff, self._batch_size)
            deleted += removed
            if removed < self._batch_size:
                break
        return deleted
