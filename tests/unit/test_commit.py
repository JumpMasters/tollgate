"""Unit tests for the commit handler: reconciliation and the self-healing late commit (§4, §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.commit import CommitHandler, commit_fingerprint
from tollgate.domain.commands import CommitCommand, CommitResult, ProviderUsage
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import (
    IdempotencyKeyReuse,
    ReservationNotHeld,
    ScopeNotAuthorized,
    UnknownModel,
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
_TTL = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)
_PRICE = ModelPrice(
    provider="anthropic",
    model="claude",
    input_micro_per_token=Decimal("1"),
    output_micro_per_token=Decimal("2"),
    cached_input_micro_per_token=Decimal("0.5"),
    cache_creation_micro_per_token=Decimal("1.25"),
)
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
    ttl_deadline=_TTL,
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
# usage (100 in / 20 cached / 50 out) at _PRICE: 80*1 + 20*0.5 + 50*2 = 190
_USAGE = ProviderUsage(input_tokens=100, output_tokens=50, cached_input_tokens=20)
_ACTUAL = 190


def _principal() -> Principal:
    return Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))


def _auth(principal_id: str = "u1") -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId(principal_id),
        scope_kind=ScopeKind.USER,
        scope_id=principal_id,
        status=CredentialStatus.ACTIVE,
    )
    return AuthContext(credential=credential, principal=_principal())


def _command(usage: ProviderUsage = _USAGE) -> CommitCommand:
    return CommitCommand(
        idempotency_key="idem-commit",
        reservation_id=ReservationId("res-1"),
        usage=usage,
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
        claim_late_commit_result: bool,
    ) -> None:
        self._stored = stored
        self._lines = lines
        self._claim_terminal_result = claim_terminal_result
        self._claim_late_commit_result = claim_late_commit_result
        self.terminal_claims: list[tuple[ReservationId, ReservationStatus]] = []
        self.late_commit_claims: list[ReservationId] = []

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("commit never inserts a reservation")

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
        self.late_commit_claims.append(reservation_id)
        return self._claim_late_commit_result

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        return None

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        raise AssertionError("this handler never reaps")


class _FakeCounterStore:
    def __init__(self, apply_splits: Mapping[str, Reconciliation] | None = None) -> None:
        self._apply_splits = dict(apply_splits or {})
        self.commit_calls: list[tuple[BudgetId, datetime, int, int]] = []
        self.apply_calls: list[tuple[BudgetId, datetime, int]] = []

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
        self.commit_calls.append((budget_id, period_start, reserved_micro, actual_micro))

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        return None

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        self.apply_calls.append((budget_id, period_start, amount_micro))
        return self._apply_splits.get(
            budget_id, Reconciliation(committed_micro=amount_micro, overage_micro=0)
        )


class _FakeLedger:
    def __init__(self) -> None:
        self.appended: list[LedgerEntry] = []

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        self.appended.extend(entries)


class _FakePrices:
    def __init__(self, price: ModelPrice | None) -> None:
        self._price = price
        self.requested: list[tuple[str, str, str]] = []

    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return None if self._price is None else PricedModel(version="2026-06-22", price=self._price)

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        self.requested.append((version, provider, model))
        return self._price


class _FakeBudgets:
    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        return ()

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        return None


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> Any:
        raise AssertionError("commit never reserves")


class _Ctx:
    def __init__(
        self,
        *,
        prices: _FakePrices,
        idempotency: _FakeIdempotency,
        reservations: _FakeReservations,
        counter_store: _FakeCounterStore,
        ledger: _FakeLedger,
    ) -> None:
        self.prices = prices
        self.budgets = _FakeBudgets()
        self.idempotency = idempotency
        self.metered_receipt = self.idempotency
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
        raise AssertionError("commit never mints a reservation id")

    def new_ledger_entry_id(self) -> LedgerEntryId:
        self._n += 1
        return LedgerEntryId(f"led-{self._n}")


def _build(
    *,
    stored: StoredReservation | None = _HELD,
    lines: Sequence[ReservationLineView] = (_USER_LINE, _ORG_LINE),
    price: ModelPrice | None = _PRICE,
    claim_terminal_result: bool = True,
    claim_late_commit_result: bool = False,
    apply_splits: Mapping[str, Reconciliation] | None = None,
    idem_outcome: ClaimOutcome = ClaimOutcome.FRESH,
    idem_response: Mapping[str, Any] | None = None,
) -> tuple[CommitHandler, _Uow]:
    ctx = _Ctx(
        prices=_FakePrices(price),
        idempotency=_FakeIdempotency(idem_outcome, idem_response),
        reservations=_FakeReservations(
            stored=stored,
            lines=lines,
            claim_terminal_result=claim_terminal_result,
            claim_late_commit_result=claim_late_commit_result,
        ),
        counter_store=_FakeCounterStore(apply_splits),
        ledger=_FakeLedger(),
    )
    uow = _Uow(ctx)
    handler = CommitHandler(uow=uow, ids=_SeqIds())
    return handler, uow


async def test_commit_reconciles_and_persists_the_envelope() -> None:
    handler, uow = _build()
    result = await handler.commit(_auth(), _command())
    assert result == CommitResult(
        reservation_id=ReservationId("res-1"), committed_micro=_ACTUAL, overage_micro=0
    )
    ctx = uow._ctx
    # the actual cost was priced at the reservation's stamped version
    assert ctx.prices.requested == [("2026-06-22", "anthropic", "claude")]
    # the identity guard fired exactly once, to committed
    assert ctx.reservations.terminal_claims == [
        (ReservationId("res-1"), ReservationStatus.COMMITTED)
    ]
    assert ctx.reservations.late_commit_claims == []
    # the walk ran in canonical lock order (org before user), moving est -> actual
    assert ctx.counter_store.commit_calls == [
        (BudgetId("b-org"), _PERIOD, 300, _ACTUAL),
        (BudgetId("b-user"), _PERIOD, 300, _ACTUAL),
    ]
    # one commit_adjust per line, no overage entries
    assert [e.kind for e in ctx.ledger.appended] == [LedgerKind.COMMIT_ADJUST] * 2
    assert all(e.delta_reserved_micro == -300 for e in ctx.ledger.appended)
    assert all(e.delta_committed_micro == _ACTUAL for e in ctx.ledger.appended)
    assert all(e.reservation_id == "res-1" for e in ctx.ledger.appended)
    assert all(e.actual_input_tokens == 100 for e in ctx.ledger.appended)
    assert all(e.actual_output_tokens == 50 for e in ctx.ledger.appended)
    assert all(e.price_book_version == "2026-06-22" for e in ctx.ledger.appended)
    assert ctx.idempotency.stored == [
        (
            "u1",
            "idem-commit",
            "succeeded",
            {"reservation_id": "res-1", "committed_micro": _ACTUAL, "overage_micro": 0},
        )
    ]
    assert uow.committed is True


async def test_commit_records_overage_above_the_estimate() -> None:
    handler, uow = _build()
    # 300 in / 0 cached / 100 out -> 300 + 200 = 500; est 300 -> committed 300, overage 200
    result = await handler.commit(
        _auth(), _command(usage=ProviderUsage(input_tokens=300, output_tokens=100))
    )
    assert result == CommitResult(
        reservation_id=ReservationId("res-1"), committed_micro=300, overage_micro=200
    )
    kinds = [e.kind for e in uow._ctx.ledger.appended]
    assert kinds == [
        LedgerKind.COMMIT_ADJUST,
        LedgerKind.OVERAGE,
        LedgerKind.COMMIT_ADJUST,
        LedgerKind.OVERAGE,
    ]
    overages = [e for e in uow._ctx.ledger.appended if e.kind is LedgerKind.OVERAGE]
    assert all(e.delta_overage_micro == 200 for e in overages)
    assert all(e.delta_reserved_micro == 0 and e.delta_committed_micro == 0 for e in overages)


async def test_commit_replays_a_stored_response_without_reapplying() -> None:
    stored_response = {"reservation_id": "res-1", "committed_micro": 190, "overage_micro": 0}
    handler, uow = _build(idem_outcome=ClaimOutcome.REPLAY, idem_response=stored_response)
    result = await handler.commit(_auth(), _command())
    assert result == CommitResult(
        reservation_id=ReservationId("res-1"), committed_micro=190, overage_micro=0
    )
    assert uow._ctx.counter_store.commit_calls == []
    assert uow._ctx.reservations.terminal_claims == []
    assert uow.committed is True


async def test_commit_rejects_a_key_reused_with_a_different_command() -> None:
    handler, uow = _build(idem_outcome=ClaimOutcome.MISMATCH)
    with pytest.raises(IdempotencyKeyReuse):
        await handler.commit(_auth(), _command())
    assert uow.rolled_back is True


async def test_commit_rejects_unknown_and_foreign_reservations_identically() -> None:
    handler_unknown, uow_unknown = _build(stored=None)
    with pytest.raises(ScopeNotAuthorized) as unknown_exc:
        await handler_unknown.commit(_auth(), _command())
    foreign = StoredReservation(
        record=ReservationRecord(
            reservation_id=ReservationId("res-1"),
            idempotency_key="idem-res",
            principal_id=PrincipalId("intruder"),
            provider="anthropic",
            model="claude",
            price_book_version="2026-06-22",
            estimated_micro=300,
            input_bound_tokens=100,
            max_output_tokens=100,
            ttl_deadline=_TTL,
            labels={},
        ),
        status=ReservationStatus.HELD,
    )
    handler_foreign, uow_foreign = _build(stored=foreign)
    with pytest.raises(ScopeNotAuthorized) as foreign_exc:
        await handler_foreign.commit(_auth(), _command())
    assert unknown_exc.value.scope == foreign_exc.value.scope == "reservation:res-1"
    assert uow_unknown.rolled_back is True and uow_foreign.rolled_back is True


async def test_commit_refuses_a_missing_stamped_price() -> None:
    handler, uow = _build(price=None)
    with pytest.raises(UnknownModel):
        await handler.commit(_auth(), _command())
    assert uow.rolled_back is True


async def test_commit_of_a_settled_reservation_is_rejected() -> None:
    # both guards lose: not held (already released/committed) and not reaped
    handler, uow = _build(claim_terminal_result=False, claim_late_commit_result=False)
    with pytest.raises(ReservationNotHeld):
        await handler.commit(_auth(), _command())
    assert uow._ctx.counter_store.commit_calls == []
    assert uow._ctx.idempotency.stored == []
    assert uow.rolled_back is True


async def test_commit_of_a_reaped_reservation_self_heals() -> None:
    splits = {
        "b-org": Reconciliation(committed_micro=_ACTUAL, overage_micro=0),
        "b-user": Reconciliation(committed_micro=100, overage_micro=90),
    }
    handler, uow = _build(
        claim_terminal_result=False, claim_late_commit_result=True, apply_splits=splits
    )
    result = await handler.commit(_auth(), _command())
    # the most-restrictive node's split: greatest overage, committed = actual - overage
    assert result == CommitResult(
        reservation_id=ReservationId("res-1"), committed_micro=100, overage_micro=90
    )
    ctx = uow._ctx
    assert ctx.reservations.late_commit_claims == [ReservationId("res-1")]
    # live-remaining application in lock order; the reap already released the hold
    assert ctx.counter_store.apply_calls == [
        (BudgetId("b-org"), _PERIOD, _ACTUAL),
        (BudgetId("b-user"), _PERIOD, _ACTUAL),
    ]
    assert ctx.counter_store.commit_calls == []
    kinds = [e.kind for e in ctx.ledger.appended]
    assert kinds == [LedgerKind.COMMIT_ADJUST, LedgerKind.COMMIT_ADJUST, LedgerKind.OVERAGE]
    assert all(e.ref == "late_commit" for e in ctx.ledger.appended)
    assert all(e.delta_reserved_micro == 0 for e in ctx.ledger.appended)
    by_kind = {(e.kind, e.budget_id): e for e in ctx.ledger.appended}
    assert by_kind[(LedgerKind.COMMIT_ADJUST, BudgetId("b-org"))].delta_committed_micro == _ACTUAL
    assert by_kind[(LedgerKind.COMMIT_ADJUST, BudgetId("b-user"))].delta_committed_micro == 100
    assert by_kind[(LedgerKind.OVERAGE, BudgetId("b-user"))].delta_overage_micro == 90
    assert uow.committed is True


async def test_commit_prices_cache_creation_tokens() -> None:
    # _PRICE has input=1, output=2, cached=0.5, cache_creation=1.25.
    # usage: 100 in / 20 cache-read / 50 out / 40 cache-creation
    #   (100-20)*1 + 20*0.5 + 50*2 + 40*1.25 = 80 + 10 + 100 + 50 = 240 (was 190 without creation)
    usage = ProviderUsage(
        input_tokens=100, output_tokens=50, cached_input_tokens=20, cache_creation_tokens=40
    )
    handler, _uow = _build()
    result = await handler.commit(_auth(), _command(usage=usage))
    assert result == CommitResult(
        reservation_id=ReservationId("res-1"), committed_micro=240, overage_micro=0
    )


def test_commit_fingerprint_is_stable_and_usage_sensitive() -> None:
    principal = _principal()
    base = _command()
    assert commit_fingerprint(principal, base) == commit_fingerprint(principal, base)
    assert commit_fingerprint(principal, base) != commit_fingerprint(
        principal, _command(usage=ProviderUsage(input_tokens=100, output_tokens=51))
    )


def test_commit_fingerprint_is_sensitive_to_cache_creation_tokens() -> None:
    principal = _principal()
    base = _command(usage=ProviderUsage(input_tokens=100, output_tokens=50))
    more = _command(
        usage=ProviderUsage(input_tokens=100, output_tokens=50, cache_creation_tokens=10)
    )
    assert commit_fingerprint(principal, base) != commit_fingerprint(principal, more)
