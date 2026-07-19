"""Unit tests for the cancel handler: release the full estimate, exactly once (§4, §5.2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.cancel import CancelHandler, cancel_fingerprint
from tollgate.domain.commands import CancelCommand, CancelResult
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import (
    IdempotencyKeyReuse,
    ReservationNotHeld,
    ScopeNotAuthorized,
)
from tollgate.domain.ids import (
    BudgetId,
    CredentialId,
    LedgerEntryId,
    OrgId,
    PrincipalId,
    ProjectId,
    ReservationId,
    TeamId,
    UserId,
)
from tollgate.domain.pricing import ModelPrice, PricedModel, Reconciliation
from tollgate.domain.records import (
    ClaimOutcome,
    IdempotencyClaim,
    LedgerEntry,
    LedgerKind,
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ResolvedProject, ScopeKind

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
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
    ttl_deadline=datetime(2026, 6, 23, 12, 10, tzinfo=UTC),
    labels={"env": "prod"},
)
_HELD = StoredReservation(record=_RECORD, status=ReservationStatus.HELD)
_USER_LINE = ReservationLineView(
    node=BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
    period_start=_PERIOD,
    amount_micro=300,
)
_ORG_LINE = ReservationLineView(
    node=BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1"),
    period_start=_PERIOD,
    amount_micro=300,
)


def _principal() -> Principal:
    return Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))


def _auth() -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("u1"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        status=CredentialStatus.ACTIVE,
    )
    return AuthContext(credential=credential, principal=_principal())


def _command(reservation_id: str = "res-1") -> CancelCommand:
    return CancelCommand(
        idempotency_key="idem-cancel", reservation_id=ReservationId(reservation_id)
    )


class _FakeIdempotency:
    def __init__(
        self, outcome: ClaimOutcome = ClaimOutcome.FRESH, response: Mapping[str, Any] | None = None
    ) -> None:
        self._claim = IdempotencyClaim(outcome, response=response)
        self.stored: list[tuple[str, str, str, dict[str, Any]]] = []

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        return self._claim

    async def store_response(
        self, principal_id: str, key: str, status: str, response: Mapping[str, Any]
    ) -> None:
        self.stored.append((principal_id, key, status, dict(response)))

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("this handler never reaps keys")


class _FakeReservations:
    def __init__(
        self,
        *,
        stored: StoredReservation | None,
        lines: Sequence[ReservationLineView],
        claim_terminal_result: bool,
    ) -> None:
        self._stored = stored
        self._lines = lines
        self._claim_terminal_result = claim_terminal_result
        self.terminal_claims: list[tuple[ReservationId, ReservationStatus]] = []

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("cancel never inserts a reservation")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        self.terminal_claims.append((reservation_id, next_status))
        return self._claim_terminal_result

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        return self._stored

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        return self._lines

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        raise AssertionError("cancel has no self-heal path")

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        return None

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        raise AssertionError("this handler never reaps")


class _FakeCounterStore:
    def __init__(self) -> None:
        self.release_calls: list[tuple[BudgetId, datetime, int]] = []

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        return None

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        return True

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        return None

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        self.release_calls.append((budget_id, period_start, amount_micro))

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        raise AssertionError("cancel never applies spend")


class _FakeLedger:
    def __init__(self) -> None:
        self.appended: list[LedgerEntry] = []

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        self.appended.extend(entries)


class _FakePrices:
    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return None

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        return None


class _FakeBudgets:
    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        return ()

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        return None


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> Any:
        raise AssertionError("cancel never reserves")


class _Ctx:
    def __init__(
        self,
        *,
        idempotency: _FakeIdempotency,
        reservations: _FakeReservations,
        counter_store: _FakeCounterStore,
        ledger: _FakeLedger,
    ) -> None:
        self.prices = _FakePrices()
        self.budgets = _FakeBudgets()
        self.idempotency = idempotency
        self.reservations = reservations
        self.ledger = ledger
        self.reserve_tx = _StubReserveTx()
        self.counter_store = counter_store


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


class _SeqIds:
    def __init__(self) -> None:
        self._n = 0

    def new_reservation_id(self) -> ReservationId:
        raise AssertionError("cancel never mints a reservation id")

    def new_ledger_entry_id(self) -> LedgerEntryId:
        self._n += 1
        return LedgerEntryId(f"led-{self._n}")


def _build(
    *,
    stored: StoredReservation | None = _HELD,
    lines: Sequence[ReservationLineView] = (_USER_LINE, _ORG_LINE),
    claim_terminal_result: bool = True,
    idem_outcome: ClaimOutcome = ClaimOutcome.FRESH,
    idem_response: Mapping[str, Any] | None = None,
) -> tuple[CancelHandler, _Uow]:
    ctx = _Ctx(
        idempotency=_FakeIdempotency(idem_outcome, idem_response),
        reservations=_FakeReservations(
            stored=stored, lines=lines, claim_terminal_result=claim_terminal_result
        ),
        counter_store=_FakeCounterStore(),
        ledger=_FakeLedger(),
    )
    uow = _Uow(ctx)
    handler = CancelHandler(uow=uow, ids=_SeqIds())
    return handler, uow


async def test_cancel_releases_every_line_and_persists_the_envelope() -> None:
    handler, uow = _build()
    result = await handler.cancel(_auth(), _command())
    assert result == CancelResult(reservation_id=ReservationId("res-1"), released_micro=300)
    ctx = uow._ctx
    assert ctx.reservations.terminal_claims == [
        (ReservationId("res-1"), ReservationStatus.RELEASED)
    ]
    # released in canonical lock order (org before user)
    assert ctx.counter_store.release_calls == [
        (BudgetId("b-org"), _PERIOD, 300),
        (BudgetId("b-user"), _PERIOD, 300),
    ]
    assert [e.kind for e in ctx.ledger.appended] == [LedgerKind.RELEASE] * 2
    assert all(e.delta_reserved_micro == -300 for e in ctx.ledger.appended)
    assert all(e.reservation_id == "res-1" for e in ctx.ledger.appended)
    assert all(e.price_book_version == "2026-06-22" for e in ctx.ledger.appended)
    assert ctx.idempotency.stored == [
        ("u1", "idem-cancel", "succeeded", {"reservation_id": "res-1", "released_micro": 300})
    ]
    assert uow.committed is True


async def test_cancel_replays_a_stored_response_without_re_releasing() -> None:
    stored_response = {"reservation_id": "res-1", "released_micro": 300}
    handler, uow = _build(idem_outcome=ClaimOutcome.REPLAY, idem_response=stored_response)
    result = await handler.cancel(_auth(), _command())
    assert result == CancelResult(reservation_id=ReservationId("res-1"), released_micro=300)
    assert uow._ctx.counter_store.release_calls == []
    assert uow.committed is True


async def test_cancel_rejects_a_key_reused_with_a_different_command() -> None:
    handler, uow = _build(idem_outcome=ClaimOutcome.MISMATCH)
    with pytest.raises(IdempotencyKeyReuse):
        await handler.cancel(_auth(), _command())
    assert uow.rolled_back is True


async def test_cancel_of_a_settled_reservation_is_rejected() -> None:
    # a reaped reservation was already released by the reaper; cancel has no self-heal (§5.4)
    handler, uow = _build(claim_terminal_result=False)
    with pytest.raises(ReservationNotHeld):
        await handler.cancel(_auth(), _command())
    assert uow._ctx.counter_store.release_calls == []
    assert uow._ctx.idempotency.stored == []
    assert uow.rolled_back is True


async def test_cancel_rejects_an_unknown_reservation_without_revealing_it() -> None:
    handler, uow = _build(stored=None)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.cancel(_auth(), _command())
    assert excinfo.value.scope == "reservation:res-1"
    assert uow.rolled_back is True


def test_cancel_fingerprint_is_stable_and_reservation_sensitive() -> None:
    principal = _principal()
    assert cancel_fingerprint(principal, _command()) == cancel_fingerprint(principal, _command())
    assert cancel_fingerprint(principal, _command()) != cancel_fingerprint(
        principal, _command("res-2")
    )
    other = Principal(user_id=UserId("u2"), team_id=TeamId("t1"), org_id=OrgId("o1"))
    assert cancel_fingerprint(principal, _command()) != cancel_fingerprint(other, _command())
