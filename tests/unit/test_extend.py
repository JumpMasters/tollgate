"""Unit tests for the extend handler: the monotonic §5.4 heartbeat (no idempotency key)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.domain.commands import ExtendCommand, ExtendResult
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import ReservationNotHeld, ScopeNotAuthorized
from tollgate.domain.ids import (
    BudgetId,
    CredentialId,
    OrgId,
    PrincipalId,
    ProjectId,
    ReservationId,
    TeamId,
    UserId,
)
from tollgate.domain.pricing import ModelPrice, PricedModel, Reconciliation
from tollgate.domain.records import (
    IdempotencyClaim,
    LedgerEntry,
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ResolvedProject, ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_RECORD = ReservationRecord(
    reservation_id=ReservationId("res-1"),
    idempotency_key="idem-res",
    principal_id=PrincipalId("u1"),
    provider="anthropic",
    model="claude",
    price_book_version="2026-06-22",
    estimated_micro=300,
    input_bound_tokens=100,
    max_output_tokens=100,
    ttl_deadline=_NOW + timedelta(seconds=600),
    labels={},
)
_HELD = StoredReservation(record=_RECORD, status=ReservationStatus.HELD)


def _auth() -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("u1"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))
    return AuthContext(credential=credential, principal=principal)


class _FakeReservations:
    def __init__(self, *, stored: StoredReservation | None, advanced: datetime | None) -> None:
        self._stored = stored
        self._advanced = advanced
        self.requested: list[tuple[ReservationId, datetime]] = []

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("extend never inserts a reservation")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError("extend never settles a reservation")

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        return self._stored

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        return ()

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        raise AssertionError("extend has no self-heal path")

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        self.requested.append((reservation_id, ttl_deadline))
        return self._advanced


class _NoIdempotency:
    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("extend is naturally idempotent and needs no key (§4)")

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        raise AssertionError("extend caches no response")


class _StubCounterStore:
    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        raise AssertionError("extend never touches balances")

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        raise AssertionError("extend never touches balances")

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        raise AssertionError("extend never touches balances")

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        raise AssertionError("extend never touches balances")

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        raise AssertionError("extend never touches balances")


class _StubLedger:
    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        raise AssertionError("extend touches no balances and appends no ledger rows")


class _StubPrices:
    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return None

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        return None


class _StubBudgets:
    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        return ()

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        return None


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> Any:
        raise AssertionError("extend never reserves")


class _Ctx:
    def __init__(self, reservations: _FakeReservations) -> None:
        self.prices = _StubPrices()
        self.budgets = _StubBudgets()
        self.idempotency = _NoIdempotency()
        self.reservations = reservations
        self.ledger = _StubLedger()
        self.reserve_tx = _StubReserveTx()
        self.counter_store = _StubCounterStore()


class _Uow:
    def __init__(self, ctx: _Ctx) -> None:
        self._ctx = ctx
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_Ctx]:
        try:
            yield self._ctx
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.committed = True


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


def _build(
    *, stored: StoredReservation | None = _HELD, advanced: datetime | None
) -> tuple[ExtendHandler, _Uow]:
    ctx = _Ctx(_FakeReservations(stored=stored, advanced=advanced))
    uow = _Uow(ctx)
    handler = ExtendHandler(uow=uow, clock=_FixedClock(), reservation_ttl_seconds=600)
    return handler, uow


async def test_extend_requests_now_plus_ttl_and_returns_the_stored_deadline() -> None:
    requested = _NOW + timedelta(seconds=600)
    handler, uow = _build(advanced=requested)
    result = await handler.extend(_auth(), ExtendCommand(reservation_id=ReservationId("res-1")))
    assert result == ExtendResult(reservation_id=ReservationId("res-1"), ttl_deadline=requested)
    assert uow._ctx.reservations.requested == [(ReservationId("res-1"), requested)]
    assert uow.committed is True


async def test_extend_honours_the_monotonic_deadline_the_store_kept() -> None:
    # the store's GREATEST kept a later deadline; the result reports what is actually stored
    kept = _NOW + timedelta(seconds=900)
    handler, _uow = _build(advanced=kept)
    result = await handler.extend(_auth(), ExtendCommand(reservation_id=ReservationId("res-1")))
    assert result.ttl_deadline == kept


async def test_extend_of_a_settled_reservation_is_rejected() -> None:
    handler, uow = _build(advanced=None)
    with pytest.raises(ReservationNotHeld):
        await handler.extend(_auth(), ExtendCommand(reservation_id=ReservationId("res-1")))
    assert uow.rolled_back is True


async def test_extend_rejects_an_unknown_reservation_without_revealing_it() -> None:
    handler, uow = _build(stored=None, advanced=None)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.extend(_auth(), ExtendCommand(reservation_id=ReservationId("res-1")))
    assert excinfo.value.scope == "reservation:res-1"
    assert uow.rolled_back is True
