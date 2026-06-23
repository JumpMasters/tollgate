"""Tests that conforming fakes satisfy the application ports.

The ports are structural (Protocols); these both document the expected shape and let mypy
verify a concrete implementation conforms.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tollgate.application.ports import (
    CounterStore,
    IdempotencyRepository,
    LedgerRepository,
    ReservationRepository,
)
from tollgate.domain.ids import BudgetId, LedgerEntryId, PrincipalId, ReservationId
from tollgate.domain.records import (
    ClaimOutcome,
    IdempotencyClaim,
    LedgerEntry,
    LedgerKind,
    ReservationLineRecord,
    ReservationRecord,
)
from tollgate.domain.reservations import ReservationStatus

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


class _FakeStore:
    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        return None

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        return amount_micro >= 0

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        return None

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        return None


class _FakeReservationRepository:
    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        return None

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        return True


class _FakeIdempotencyRepository:
    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        return IdempotencyClaim(ClaimOutcome.FRESH)

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        return None


class _FakeLedgerRepository:
    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        return None


async def test_fake_conforms_to_counter_store() -> None:
    store: CounterStore = _FakeStore()
    await store.ensure_period(BudgetId("b1"), _PERIOD)
    assert await store.reserve(BudgetId("b1"), _PERIOD, 10)
    await store.commit(BudgetId("b1"), _PERIOD, 10, 8)
    await store.release(BudgetId("b1"), _PERIOD, 2)


async def test_fakes_conform_to_the_repository_ports() -> None:
    reservations: ReservationRepository = _FakeReservationRepository()
    idempotency: IdempotencyRepository = _FakeIdempotencyRepository()
    ledger: LedgerRepository = _FakeLedgerRepository()

    record = ReservationRecord(
        reservation_id=ReservationId("r1"),
        idempotency_key="idem-1",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="v1",
        estimated_micro=100,
        input_bound_tokens=50,
        max_output_tokens=50,
        ttl_deadline=_PERIOD,
        labels={"team": "blue"},
    )
    line = ReservationLineRecord(
        reservation_id=ReservationId("r1"),
        budget_id=BudgetId("b1"),
        period_start=_PERIOD,
        amount_micro=100,
    )
    await reservations.insert(record, [line])
    assert await reservations.claim_terminal(ReservationId("r1"), ReservationStatus.COMMITTED)

    claim = await idempotency.claim("idem-1", "fp")
    assert claim.outcome is ClaimOutcome.FRESH
    await idempotency.store_response("idem-1", "succeeded", {"reservation_id": "r1"})

    await ledger.append(
        [
            LedgerEntry(
                entry_id=LedgerEntryId("e1"),
                kind=LedgerKind.RESERVE,
                budget_id=BudgetId("b1"),
                period_start=_PERIOD,
                reservation_id=ReservationId("r1"),
                delta_reserved_micro=100,
            )
        ]
    )
