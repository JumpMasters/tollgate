"""Unit tests for the reaper handlers: reap expired reservations, delete aged keys (§5.4, §5.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

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

    async def claim_next_expired(self, now: datetime) -> StoredReservation | None:
        self.claim_now.append(now)
        return self._expired.pop(0) if self._expired else None


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
    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("the reservation reaper never claims idempotency keys")

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        raise AssertionError("the reservation reaper never claims idempotency keys")

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("the reservation reaper never claims idempotency keys")


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> ReserveOutcome:
        raise AssertionError("the reservation reaper never reserves")


class _Ctx:
    def __init__(self, reservations: _FakeReservations) -> None:
        self.prices = _StubPrices()
        self.budgets = _StubBudgets()
        self.idempotency = _StubIdempotency()
        self.reservations = reservations
        self.ledger = _FakeLedger()
        self.reserve_tx = _StubReserveTx()
        self.counter_store = _FakeCounterStore()


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

    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("the idempotency reaper never claims a key")

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
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

    async def claim_next_expired(self, now: datetime) -> StoredReservation | None:
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
    """A ``CommandContext`` stubbed everywhere except ``idempotency`` (§5.5)."""

    def __init__(self, idempotency: _FakeIdempotency) -> None:
        self.prices = _StubPrices()
        self.budgets = _StubBudgets()
        self.idempotency = idempotency
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
    handler = IdempotencyReaperHandler(uow=uow, clock=_ClockAt(_NOW), ttl_hours=24, batch_size=500)
    deleted = await handler.run_once()
    assert deleted == 1137
    assert uow.begins == 3  # one bounded transaction per batch
    # cutoff is now - 24h, applied to every batch
    cutoff = _NOW - timedelta(hours=24)
    assert idem.calls == [(cutoff, 500), (cutoff, 500), (cutoff, 500)]


async def test_idempotency_reaper_with_nothing_expired_stops_after_one_short_batch() -> None:
    idem = _FakeIdempotency([0])
    uow = _IdemUow(_IdemCtx(idem))
    handler = IdempotencyReaperHandler(uow=uow, clock=_ClockAt(_NOW), ttl_hours=24, batch_size=500)
    assert await handler.run_once() == 0
    assert uow.begins == 1
