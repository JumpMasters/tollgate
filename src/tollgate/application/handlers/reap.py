"""The reaper handlers: release abandoned reservations and delete aged idempotency keys.

Reaping is a system-driven cancel. ``ReservationReaperHandler.run_once`` polls
``claim_next_expired`` (a ``FOR UPDATE SKIP LOCKED`` claim-and-reap), and for each reaped
reservation releases its held estimate on every line in the canonical lock order and appends
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
    """Releases held reservations past their TTL, one bounded transaction each."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        batch_size: int,
        max_reap_attempts: int = 5,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._batch_size = batch_size
        self._max_reap_attempts = max_reap_attempts
        # Cross-tick poison-row bookkeeping (#91). Attempt counts accumulate across ticks; a row
        # that fails ``max_reap_attempts`` times is quarantined — excluded from every future claim
        # this process — so it stops recirculating at the queue head and stranding its estimate
        # unseen. This state is per-process (a restart re-attempts, a fresh chance if contention
        # cleared); a durable dead-letter would need a persisted attempt column.
        self._reap_attempts: dict[ReservationId, int] = {}
        self._quarantined: set[ReservationId] = set()

    async def run_once(self) -> ReapReport:
        """Reap expired reservations, each in its own SKIP LOCKED tx, isolating per-item failures.

        A fixed ``now`` (captured once) keeps the tick deterministic: a reservation that expires
        mid-tick is caught on the next one. Each per-item transaction claims-and-reaps one
        reservation, releases every line, and appends the ``reap`` rows, committing them
        atomically. A reap that raises is caught so one persistently failing reservation cannot
        abort the whole tick and starve everything behind it (#74): its id is excluded from the
        rest of this tick (its rolled-back status flip would otherwise put it back at the queue
        head) and the reaper moves to the next candidate. A row that fails this way across
        ``max_reap_attempts`` ticks is *quarantined* — excluded from every future claim and logged
        as an error — so a permanent poison row surfaces instead of recirculating forever and
        silently stranding its estimate (#91). A failure in the *claim* itself (no id yet) is a
        datastore problem, not one poison row, so it propagates for the runner to handle. The tick
        is bounded to ``batch_size`` attempts; the next tick continues.
        """
        now = self._clock.now()
        reaped = 0
        failed = 0
        # Start from the durable quarantine set so poison rows never re-enter the candidate window.
        skip: list[ReservationId] = list(self._quarantined)
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
                self._reap_attempts.pop(reservation_id, None)  # a clean reap clears its history
            except Exception:
                if reservation_id is None:
                    raise  # the claim failed, not a single reap; let the runner handle it
                self._record_reap_failure(reservation_id)
                skip.append(reservation_id)
                failed += 1
        return ReapReport(reaped=reaped, failed=failed)

    def _record_reap_failure(self, reservation_id: ReservationId) -> None:
        """Count a per-item reap failure and quarantine the row once it exhausts its attempts."""
        attempts = self._reap_attempts.get(reservation_id, 0) + 1
        if attempts >= self._max_reap_attempts:
            self._quarantined.add(reservation_id)
            self._reap_attempts.pop(reservation_id, None)
            logger.error(
                "reservation reap failed for %s %d times; quarantining it (its reserved estimate "
                "is stranded until it is resolved) — investigate",
                reservation_id,
                attempts,
            )
        else:
            self._reap_attempts[reservation_id] = attempts
            logger.exception(
                "reservation reap failed for %s (attempt %d/%d); skipping it this tick",
                reservation_id,
                attempts,
                self._max_reap_attempts,
            )


class IdempotencyReaperHandler:
    """Batch-deletes idempotency keys past their TTL, one bounded transaction each."""

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
