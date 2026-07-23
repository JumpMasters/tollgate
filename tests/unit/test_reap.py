"""Unit tests for the reaper handlers: reap expired reservations, delete aged keys."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tollgate.application.handlers.reap import (
    IdempotencyReaperHandler,
    ReapReport,
    ReservationReaperHandler,
)
from tollgate.domain.credentials import Principal
from tollgate.domain.ids import BudgetId, LedgerEntryId, PrincipalId, ProjectId, ReservationId
from tollgate.domain.pricing import ModelPrice, PricedModel, Reconciliation
from tollgate.domain.records import (
    IdempotencyClaim,
    LedgerEntry,
    LedgerKind,
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ReserveOutcome, ResolvedProject, ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


def _stored(rid: str) -> StoredReservation:
    record = ReservationRecord(
        reservation_id=ReservationId(rid),
        idempotency_key=f"idem-{rid}",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="pb-1",
        estimated_micro=300,
        input_bound_tokens=100,
        max_output_tokens=100,
        ttl_deadline=_NOW,
        labels={},
    )
    return StoredReservation(record=record, status=ReservationStatus.REAPED)


_USER_LINE = ReservationLineView(
    node=BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
    period_start=_PERIOD,
    amount_micro=300,
)
_ORG_LINE = ReservationLineView(
    node=BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1"), period_start=_PERIOD, amount_micro=300
)


class _ClockAt:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeReservations:
    def __init__(
        self, expired: list[StoredReservation], lines: Sequence[ReservationLineView]
    ) -> None:
        self._expired = list(expired)
        self._lines = lines
        self.claim_now: list[datetime] = []

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("the reservation reaper never inserts a reservation")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError(
            "the reservation reaper claims via claim_next_expired, not claim_terminal"
        )

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        raise AssertionError("the reservation reaper never looks up by id")

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        return self._lines

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        raise AssertionError("the reservation reaper has no self-heal path")

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        raise AssertionError("the reservation reaper never advances a TTL")

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        self.claim_now.append(now)
        for i, stored in enumerate(self._expired):
            if stored.record.reservation_id not in exclude_ids:
                return self._expired.pop(i)
        return None


class _FakeCounterStore:
    def __init__(self) -> None:
        self.release_calls: list[tuple[BudgetId, datetime, int]] = []

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        raise AssertionError("the reservation reaper never seeds a period")

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        raise AssertionError("the reservation reaper never reserves")

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        raise AssertionError("the reservation reaper never commits")

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        self.release_calls.append((budget_id, period_start, amount_micro))

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        raise AssertionError("the reservation reaper never applies spend directly")


class _FakeLedger:
    def __init__(self) -> None:
        self.appended: list[LedgerEntry] = []

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        self.appended.extend(entries)


class _StubPrices:
    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        raise AssertionError("the reservation reaper never resolves prices")

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        raise AssertionError("the reservation reaper never resolves prices")


class _StubBudgets:
    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        raise AssertionError("the reservation reaper never resolves budgets")

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        raise AssertionError("the reservation reaper never resolves budgets")


class _StubIdempotency:
    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("the reservation reaper never claims idempotency keys")

    async def store_response(
        self, principal_id: str, key: str, response: Mapping[str, Any]
    ) -> None:
        raise AssertionError("the reservation reaper never claims idempotency keys")

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("the reservation reaper never claims idempotency keys")


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> ReserveOutcome:
        raise AssertionError("the reservation reaper never reserves")


class _Ctx:
    def __init__(
        self, reservations: _FakeReservations, counter_store: _FakeCounterStore | None = None
    ) -> None:
        self.prices = _StubPrices()
        self.budgets = _StubBudgets()
        self.idempotency = _StubIdempotency()
        self.metered_receipt = self.idempotency
        self.reservations = reservations
        self.ledger = _FakeLedger()
        self.reserve_tx = _StubReserveTx()
        self.counter_store = counter_store or _FakeCounterStore()


class _Uow:
    def __init__(self, ctx: _Ctx) -> None:
        self._ctx = ctx
        self.begins = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_Ctx]:
        self.begins += 1
        yield self._ctx


class _SeqIds:
    def __init__(self) -> None:
        self._n = 0

    def new_ledger_entry_id(self) -> LedgerEntryId:
        self._n += 1
        return LedgerEntryId(f"led-{self._n}")

    def new_reservation_id(self) -> ReservationId:
        raise AssertionError("the reaper never mints a reservation id")


async def test_reaper_reaps_each_expired_and_releases_lines_in_lock_order() -> None:
    reservations = _FakeReservations(
        [_stored("r-1"), _stored("r-2")], lines=(_USER_LINE, _ORG_LINE)
    )
    ctx = _Ctx(reservations)
    handler = ReservationReaperHandler(
        uow=_Uow(ctx), clock=_ClockAt(_NOW), ids=_SeqIds(), batch_size=100
    )
    report = await handler.run_once()
    assert report == ReapReport(reaped=2)
    # each reservation released both lines, org before user (canonical lock order)
    assert ctx.counter_store.release_calls == [
        (BudgetId("b-org"), _PERIOD, 300),
        (BudgetId("b-user"), _PERIOD, 300),
        (BudgetId("b-org"), _PERIOD, 300),
        (BudgetId("b-user"), _PERIOD, 300),
    ]
    assert [e.kind for e in ctx.ledger.appended] == [LedgerKind.REAP] * 4
    assert all(e.delta_reserved_micro == -300 for e in ctx.ledger.appended)
    assert all(
        e.provider == "anthropic" and e.price_book_version == "pb-1" for e in ctx.ledger.appended
    )
    assert {e.reservation_id for e in ctx.ledger.appended} == {"r-1", "r-2"}
    assert reservations.claim_now == [_NOW, _NOW, _NOW]  # 2 claims + the empty poll that ends it


_POISON_BUDGET = BudgetId("b-poison")


class _PoisonCounterStore(_FakeCounterStore):
    """release() raises for the poison node, modelling a reap that reliably fails and rolls back."""

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        if budget_id == _POISON_BUDGET:
            raise RuntimeError("release times out on a hot balance row")
        await super().release(budget_id, period_start, amount_micro)


class _StarvingReservations(_FakeReservations):
    """A poison reservation (oldest, reap always rolls back → stays held) ahead of healthy ones.

    Models the real queue: the poison is returned first every claim UNLESS it is excluded, since
    its rolled-back status flip leaves it held with the oldest deadline; healthy reservations are
    consumed once reaped.
    """

    def __init__(self, poison: ReservationId, healthy: list[ReservationId]) -> None:
        super().__init__([], lines=())
        self._poison = poison
        self._healthy = list(healthy)
        self.claim_excludes: list[list[ReservationId]] = []

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        self.claim_excludes.append(list(exclude_ids))
        if self._poison not in exclude_ids:
            return _stored(str(self._poison))
        if self._healthy:
            return _stored(str(self._healthy.pop(0)))
        return None

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        budget = _POISON_BUDGET if reservation_id == self._poison else BudgetId("b-user")
        return (
            ReservationLineView(
                node=BudgetNode(budget, ScopeKind.USER, "u1"),
                period_start=_PERIOD,
                amount_micro=300,
            ),
        )


async def test_reaper_isolates_a_poison_reservation_and_reaps_the_rest() -> None:
    # One reservation whose reap reliably fails must not abort the tick or starve the queue behind
    # it: it is excluded after its failure and the healthy reservations are still reaped (#74).
    reservations = _StarvingReservations(
        poison=ReservationId("r-poison"),
        healthy=[ReservationId("r-good-1"), ReservationId("r-good-2")],
    )
    ctx = _Ctx(reservations, counter_store=_PoisonCounterStore())
    handler = ReservationReaperHandler(
        uow=_Uow(ctx), clock=_ClockAt(_NOW), ids=_SeqIds(), batch_size=100
    )
    report = await handler.run_once()
    assert report == ReapReport(reaped=2, failed=1)
    # the poison is excluded on every claim after its first (failed) attempt
    assert reservations.claim_excludes == [[], ["r-poison"], ["r-poison"], ["r-poison"]]
    # only the two healthy reservations released their lines
    assert ctx.counter_store.release_calls == [
        (BudgetId("b-user"), _PERIOD, 300),
        (BudgetId("b-user"), _PERIOD, 300),
    ]
    assert {e.reservation_id for e in ctx.ledger.appended} == {"r-good-1", "r-good-2"}


async def test_reaper_quarantines_a_persistent_poison_row_after_max_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A reservation whose reap fails every tick would otherwise recirculate at the queue head
    # forever — its rolled-back status flip leaves it held with the oldest deadline — stranding its
    # estimate silently (#91). After max_reap_attempts failed ticks the reaper quarantines it:
    # excludes it from future claims (so it stops blocking and wasting attempts) and logs an error
    # so a permanent poison row surfaces instead of recirculating unseen.
    import logging

    poison = ReservationId("r-poison")
    reservations = _StarvingReservations(poison=poison, healthy=[])
    handler = ReservationReaperHandler(
        uow=_Uow(_Ctx(reservations, counter_store=_PoisonCounterStore())),
        clock=_ClockAt(_NOW),
        ids=_SeqIds(),
        batch_size=100,
        max_reap_attempts=3,
    )
    # Three ticks each fail on the poison (attempts 1, 2, 3); the third crosses the threshold.
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            report = await handler.run_once()
            assert report.failed == 1
    assert any("quarantin" in r.message.lower() for r in caplog.records)

    # From now on the poison is excluded from the claim, so a tick finds nothing and does not fail.
    reservations.claim_excludes.clear()
    report = await handler.run_once()
    assert report == ReapReport(reaped=0, failed=0)
    assert reservations.claim_excludes[0] == [poison]  # quarantined id excluded on the next claim


async def test_reaper_does_not_quarantine_healthy_reservations() -> None:
    # A tick that reaps cleanly never accrues attempts, so healthy reservations are never excluded.
    reservations = _FakeReservations([_stored("r-1"), _stored("r-2")], lines=(_USER_LINE,))
    handler = ReservationReaperHandler(
        uow=_Uow(_Ctx(reservations)),
        clock=_ClockAt(_NOW),
        ids=_SeqIds(),
        batch_size=100,
        max_reap_attempts=3,
    )
    report = await handler.run_once()
    assert report == ReapReport(reaped=2, failed=0)


async def test_reaper_propagates_a_claim_failure() -> None:
    # A failure in the claim itself (no reservation id yet) is a datastore problem, not one poison
    # row, so it propagates for the runner's backoff/escalation to handle (#74/#75).
    class _ClaimFails(_FakeReservations):
        async def claim_next_expired(
            self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
        ) -> StoredReservation | None:
            raise RuntimeError("datastore unreachable")

    ctx = _Ctx(_ClaimFails([], lines=(_USER_LINE,)))
    handler = ReservationReaperHandler(
        uow=_Uow(ctx), clock=_ClockAt(_NOW), ids=_SeqIds(), batch_size=100
    )
    with pytest.raises(RuntimeError, match="datastore unreachable"):
        await handler.run_once()


async def test_reaper_stops_at_batch_size() -> None:
    reservations = _FakeReservations([_stored(f"r-{i}") for i in range(5)], lines=(_USER_LINE,))
    ctx = _Ctx(reservations)
    handler = ReservationReaperHandler(
        uow=_Uow(ctx), clock=_ClockAt(_NOW), ids=_SeqIds(), batch_size=3
    )
    report = await handler.run_once()
    assert report == ReapReport(reaped=3)  # capped; the remaining two drain next tick
    assert len(reservations.claim_now) == 3  # never polls past the cap


async def test_reaper_with_nothing_expired_does_nothing() -> None:
    ctx = _Ctx(_FakeReservations([], lines=(_USER_LINE,)))
    handler = ReservationReaperHandler(
        uow=_Uow(ctx), clock=_ClockAt(_NOW), ids=_SeqIds(), batch_size=100
    )
    report = await handler.run_once()
    assert report == ReapReport(reaped=0)
    assert ctx.ledger.appended == []


class _FakeIdempotency:
    def __init__(self, removals: list[int]) -> None:
        self._removals = list(removals)
        self.calls: list[tuple[datetime, int]] = []

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("the idempotency reaper never claims a key")

    async def store_response(
        self, principal_id: str, key: str, response: Mapping[str, Any]
    ) -> None:
        raise AssertionError("the idempotency reaper never stores a response")

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        self.calls.append((cutoff, limit))
        return self._removals.pop(0) if self._removals else 0


class _StubReservationsUntouched:
    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        raise AssertionError("the idempotency reaper never touches reservations")

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        raise AssertionError("the idempotency reaper never touches reservations")


class _StubLedgerUntouched:
    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        raise AssertionError("the idempotency reaper never appends ledger rows")


class _StubCounterStoreUntouched:
    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        raise AssertionError("the idempotency reaper never touches the counter store")

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        raise AssertionError("the idempotency reaper never touches the counter store")

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        raise AssertionError("the idempotency reaper never touches the counter store")

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        raise AssertionError("the idempotency reaper never touches the counter store")

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        raise AssertionError("the idempotency reaper never touches the counter store")


class _IdemCtx:
    """A ``CommandContext`` stubbed everywhere except ``idempotency``."""

    def __init__(self, idempotency: _FakeIdempotency) -> None:
        self.prices = _StubPrices()
        self.budgets = _StubBudgets()
        self.idempotency = idempotency
        self.metered_receipt = idempotency
        self.reservations = _StubReservationsUntouched()
        self.ledger = _StubLedgerUntouched()
        self.reserve_tx = _StubReserveTx()
        self.counter_store = _StubCounterStoreUntouched()


class _IdemUow:
    def __init__(self, ctx: _IdemCtx) -> None:
        self._ctx = ctx
        self.begins = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_IdemCtx]:
        self.begins += 1
        yield self._ctx


async def test_idempotency_reaper_loops_batches_until_short_and_sums() -> None:
    idem = _FakeIdempotency([500, 500, 137])  # two full batches then a short one
    uow = _IdemUow(_IdemCtx(idem))
    handler = IdempotencyReaperHandler(
        uow=uow, clock=_ClockAt(_NOW), ttl_hours=24, batch_size=500, max_batches_per_tick=100
    )
    deleted = await handler.run_once()
    assert deleted == 1137
    assert uow.begins == 3  # one bounded transaction per batch
    # cutoff is now - 24h, applied to every batch
    cutoff = _NOW - timedelta(hours=24)
    assert idem.calls == [(cutoff, 500), (cutoff, 500), (cutoff, 500)]


async def test_idempotency_reaper_with_nothing_expired_stops_after_one_short_batch() -> None:
    idem = _FakeIdempotency([0])
    uow = _IdemUow(_IdemCtx(idem))
    handler = IdempotencyReaperHandler(
        uow=uow, clock=_ClockAt(_NOW), ttl_hours=24, batch_size=500, max_batches_per_tick=100
    )
    assert await handler.run_once() == 0
    assert uow.begins == 1


async def test_idempotency_reaper_caps_batches_per_tick() -> None:
    # A backlog that never returns a short batch must not run unbounded: the tick stops after
    # max_batches_per_tick and the next tick continues the drain, keeping shutdown graceful (#73).
    idem = _FakeIdempotency([2, 2, 2, 2, 2])  # five full batches available
    uow = _IdemUow(_IdemCtx(idem))
    handler = IdempotencyReaperHandler(
        uow=uow, clock=_ClockAt(_NOW), ttl_hours=24, batch_size=2, max_batches_per_tick=3
    )
    deleted = await handler.run_once()
    assert deleted == 6  # 3 batches x 2, not all five
    assert uow.begins == 3  # capped at max_batches_per_tick
