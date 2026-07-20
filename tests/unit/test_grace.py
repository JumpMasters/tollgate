"""Unit tests for the grace backfill handler (§5.6, ADR 0030)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.grace import (
    GraceBackfillHandler,
    grace_backfill_fingerprint,
)
from tollgate.domain.commands import GraceBackfillCommand, GraceBackfillResult, ProviderUsage
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import (
    BudgetNotFound,
    IdempotencyKeyReuse,
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

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
_PRICE = ModelPrice(
    provider="anthropic",
    model="claude",
    input_micro_per_token=Decimal("1"),
    output_micro_per_token=Decimal("2"),
    cached_input_micro_per_token=Decimal("0.5"),
    cache_creation_micro_per_token=Decimal("1.25"),
)
_PRICED = PricedModel(version="2026-06-22", price=_PRICE)
_USER_NODE = BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1")
_ORG_NODE = BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")
_PROJECT_NODE = BudgetNode(BudgetId("b-proj"), ScopeKind.PROJECT, "proj-1")
# usage (100 in / 20 cached / 50 out) at _PRICE: 80*1 + 20*0.5 + 50*2 = 190
_USAGE = ProviderUsage(input_tokens=100, output_tokens=50, cached_input_tokens=20)
_ACTUAL = 190


def _principal() -> Principal:
    return Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))


def _auth(scope_kind: ScopeKind = ScopeKind.USER, scope_id: str = "u1") -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("u1"),
        scope_kind=scope_kind,
        scope_id=scope_id,
        status=CredentialStatus.ACTIVE,
    )
    return AuthContext(credential=credential, principal=_principal())


def _command(
    usage: ProviderUsage = _USAGE, project_id: ProjectId | None = None
) -> GraceBackfillCommand:
    return GraceBackfillCommand(
        idempotency_key="idem-grace",
        provider="anthropic",
        model="claude",
        usage=usage,
        project_id=project_id,
    )


class _FakeIdempotency:
    def __init__(
        self, outcome: ClaimOutcome = ClaimOutcome.FRESH, response: Mapping[str, Any] | None = None
    ) -> None:
        self._claim = IdempotencyClaim(outcome, response=response)
        self.stored: list[tuple[str, str, dict[str, Any]]] = []

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        return self._claim

    async def store_response(
        self, principal_id: str, key: str, response: Mapping[str, Any]
    ) -> None:
        self.stored.append((principal_id, key, dict(response)))

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("this handler never reaps keys")


class _TripwireIdempotency:
    """Stands in for the reaped idempotency store. Meter/grace must dedup via the durable
    metered_receipt (#92), so any call here is a regression to the double-applying path."""

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        raise AssertionError("meter/grace must dedup via metered_receipt, not idempotency (#92)")

    async def store_response(
        self, principal_id: str, key: str, response: Mapping[str, Any]
    ) -> None:
        raise AssertionError("meter/grace must dedup via metered_receipt, not idempotency (#92)")

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("meter/grace never reap")


class _FakeCounterStore:
    def __init__(self, apply_splits: Mapping[str, Reconciliation] | None = None) -> None:
        self._apply_splits = dict(apply_splits or {})
        self.ensured: list[tuple[BudgetId, datetime]] = []
        self.apply_calls: list[tuple[BudgetId, datetime, int]] = []

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        self.ensured.append((budget_id, period_start))

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        return True

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        raise AssertionError("grace backfill never moves reserved spend")

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        raise AssertionError("grace backfill never releases")

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


class _StubPrices:
    def __init__(self, priced: PricedModel | None) -> None:
        self._priced = priced

    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return self._priced

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        raise AssertionError("grace has no stamped version; it resolves the current price")


class _StubBudgets:
    def __init__(
        self,
        ancestry: Sequence[BudgetNode] = (),
        project: ResolvedProject | None = None,
    ) -> None:
        self._ancestry = ancestry
        self._project = project

    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        return self._ancestry

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        return self._project


class _StubReservations:
    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("grace backfill creates no reservation (§5.6)")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError("grace backfill claims no reservation")

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        return None

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        return ()

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        return False

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        return None

    async def claim_next_expired(
        self, now: datetime, exclude_ids: Sequence[ReservationId] = ()
    ) -> StoredReservation | None:
        raise AssertionError("this handler never reaps")


class _StubReserveTx:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> Any:
        raise AssertionError("grace backfill never reserves")


class _Ctx:
    def __init__(
        self,
        *,
        prices: _StubPrices,
        budgets: _StubBudgets,
        idempotency: _FakeIdempotency,
        counter_store: _FakeCounterStore,
        ledger: _FakeLedger,
    ) -> None:
        self.prices = prices
        self.budgets = budgets
        self.metered_receipt = idempotency
        self.idempotency = _TripwireIdempotency()
        self.reservations = _StubReservations()
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


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _SeqIds:
    def __init__(self) -> None:
        self._n = 0

    def new_reservation_id(self) -> ReservationId:
        raise AssertionError("grace backfill mints no reservation id")

    def new_ledger_entry_id(self) -> LedgerEntryId:
        self._n += 1
        return LedgerEntryId(f"led-{self._n}")


def _build(
    *,
    priced: PricedModel | None = _PRICED,
    ancestry: Sequence[BudgetNode] = (_USER_NODE, _ORG_NODE),
    project: ResolvedProject | None = None,
    apply_splits: Mapping[str, Reconciliation] | None = None,
    idem_outcome: ClaimOutcome = ClaimOutcome.FRESH,
    idem_response: Mapping[str, Any] | None = None,
) -> tuple[GraceBackfillHandler, _Uow]:
    ctx = _Ctx(
        prices=_StubPrices(priced),
        budgets=_StubBudgets(ancestry, project),
        idempotency=_FakeIdempotency(idem_outcome, idem_response),
        counter_store=_FakeCounterStore(apply_splits),
        ledger=_FakeLedger(),
    )
    uow = _Uow(ctx)
    handler = GraceBackfillHandler(uow=uow, clock=_FixedClock(), ids=_SeqIds())
    return handler, uow


async def test_backfill_records_spend_on_every_applicable_node() -> None:
    splits = {
        "b-org": Reconciliation(committed_micro=_ACTUAL, overage_micro=0),
        "b-user": Reconciliation(committed_micro=150, overage_micro=40),
    }
    handler, uow = _build(apply_splits=splits)
    result = await handler.backfill(_auth(), _command())
    assert result == GraceBackfillResult(actual_micro=_ACTUAL, price_book_version="2026-06-22")
    ctx = uow._ctx
    # lazy period roll then live-remaining application, in canonical lock order (§5.3/§5.5)
    assert ctx.counter_store.ensured == [
        (BudgetId("b-org"), _PERIOD),
        (BudgetId("b-user"), _PERIOD),
    ]
    assert ctx.counter_store.apply_calls == [
        (BudgetId("b-org"), _PERIOD, _ACTUAL),
        (BudgetId("b-user"), _PERIOD, _ACTUAL),
    ]
    # one grace_backfill row per node carrying both deltas; no reservation to reference
    assert [e.kind for e in ctx.ledger.appended] == [LedgerKind.GRACE_BACKFILL] * 2
    by_budget = {e.budget_id: e for e in ctx.ledger.appended}
    assert by_budget[BudgetId("b-org")].delta_committed_micro == _ACTUAL
    assert by_budget[BudgetId("b-org")].delta_overage_micro == 0
    assert by_budget[BudgetId("b-user")].delta_committed_micro == 150
    assert by_budget[BudgetId("b-user")].delta_overage_micro == 40
    assert all(e.reservation_id is None for e in ctx.ledger.appended)
    assert all(e.delta_reserved_micro == 0 for e in ctx.ledger.appended)
    assert all(e.actual_input_tokens == 100 for e in ctx.ledger.appended)
    assert all(e.actual_output_tokens == 50 for e in ctx.ledger.appended)
    assert all(e.provider == "anthropic" for e in ctx.ledger.appended)
    assert all(e.price_book_version == "2026-06-22" for e in ctx.ledger.appended)
    assert ctx.metered_receipt.stored == [
        (
            "u1",
            "idem-grace",
            {"actual_micro": _ACTUAL, "price_book_version": "2026-06-22"},
        )
    ]
    assert uow.committed is True


async def test_backfill_replays_a_stored_response_without_reapplying() -> None:
    stored = {"actual_micro": 190, "price_book_version": "2026-06-01"}
    handler, uow = _build(idem_outcome=ClaimOutcome.REPLAY, idem_response=stored)
    result = await handler.backfill(_auth(), _command())
    assert result == GraceBackfillResult(actual_micro=190, price_book_version="2026-06-01")
    assert uow._ctx.counter_store.apply_calls == []
    assert uow.committed is True


async def test_backfill_rejects_a_key_reused_with_a_different_command() -> None:
    handler, uow = _build(idem_outcome=ClaimOutcome.MISMATCH)
    with pytest.raises(IdempotencyKeyReuse):
        await handler.backfill(_auth(), _command())
    assert uow.rolled_back is True


async def test_backfill_denies_an_unknown_model_and_rolls_back() -> None:
    handler, uow = _build(priced=None)
    with pytest.raises(UnknownModel):
        await handler.backfill(_auth(), _command())
    assert uow.rolled_back is True


async def test_backfill_with_no_governing_budget_is_rejected() -> None:
    # nothing to reconcile against -> BudgetNotFound (default-deny, ADR 0030)
    handler, uow = _build(ancestry=())
    with pytest.raises(BudgetNotFound):
        await handler.backfill(_auth(), _command())
    assert uow.rolled_back is True


async def test_backfill_includes_an_authorized_project_budget() -> None:
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE, _ORG_NODE), project=project)
    await handler.backfill(_auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("proj-1")))
    applied_budgets = [call[0] for call in uow._ctx.counter_store.apply_calls]
    assert BudgetId("b-proj") in applied_budgets


async def test_backfill_rejects_a_project_outside_the_credential_scope() -> None:
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE,), project=project)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.backfill(
            _auth(ScopeKind.USER, "u1"), _command(project_id=ProjectId("proj-1"))
        )
    assert excinfo.value.scope == "project:proj-1"
    assert uow.rolled_back is True


async def test_backfill_rejects_an_unknown_project_without_revealing_it() -> None:
    handler, uow = _build(ancestry=(_USER_NODE,), project=None)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.backfill(_auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("ghost")))
    assert excinfo.value.scope == "project:ghost"
    assert uow.rolled_back is True


def test_grace_backfill_fingerprint_is_stable_and_command_sensitive() -> None:
    principal = _principal()
    base = _command()
    assert grace_backfill_fingerprint(principal, base) == grace_backfill_fingerprint(
        principal, base
    )
    assert grace_backfill_fingerprint(principal, base) != grace_backfill_fingerprint(
        principal, _command(usage=ProviderUsage(input_tokens=100, output_tokens=51))
    )
    assert grace_backfill_fingerprint(principal, base) != grace_backfill_fingerprint(
        principal, _command(project_id=ProjectId("proj-1"))
    )


def test_grace_fingerprint_is_sensitive_to_cache_creation_tokens() -> None:
    principal = _principal()
    base = _command(usage=ProviderUsage(input_tokens=100, output_tokens=50))
    more = _command(
        usage=ProviderUsage(input_tokens=100, output_tokens=50, cache_creation_tokens=10)
    )
    assert grace_backfill_fingerprint(principal, base) != grace_backfill_fingerprint(
        principal, more
    )
