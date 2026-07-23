"""Unit tests for the metering command handler (ADR 0037)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.meter import MeterHandler, meter_fingerprint
from tollgate.domain.commands import MeterCommand, MeterResult, ProviderUsage
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
from tollgate.domain.pricing import ModelPrice, PricedModel, Reconciliation, actual_micro
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
_LABELS = {"team": "growth"}


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


_BASE_COMMAND = MeterCommand(
    idempotency_key="idem-meter",
    provider="anthropic",
    model="claude",
    usage=_USAGE,
    labels=_LABELS,
)


def _command(**overrides: Any) -> MeterCommand:
    return dataclasses.replace(_BASE_COMMAND, **overrides)


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
        raise AssertionError("metering never reserves — it never denies")

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        raise AssertionError("metering has no reservation to commit")

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        raise AssertionError("metering never releases")

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
        raise AssertionError("meter has no stamped version; it resolves the current price")


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
        raise AssertionError("metering creates no reservation")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError("metering claims no reservation")

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
        raise AssertionError("metering never reserves")


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
        raise AssertionError("metering mints no reservation id")

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
) -> tuple[MeterHandler, _Uow]:
    ctx = _Ctx(
        prices=_StubPrices(priced),
        budgets=_StubBudgets(ancestry, project),
        idempotency=_FakeIdempotency(idem_outcome, idem_response),
        counter_store=_FakeCounterStore(apply_splits),
        ledger=_FakeLedger(),
    )
    uow = _Uow(ctx)
    handler = MeterHandler(uow=uow, clock=_FixedClock(), ids=_SeqIds())
    return handler, uow


async def test_meter_records_spend_never_denies_and_books_overage() -> None:
    # b-user's live remaining is less than the actual: committed is capped, the rest is overage.
    splits = {
        "b-org": Reconciliation(committed_micro=_ACTUAL, overage_micro=0),
        "b-user": Reconciliation(committed_micro=150, overage_micro=40),
    }
    handler, uow = _build(apply_splits=splits)
    result = await handler.meter(_auth(), _command())
    assert result == MeterResult(actual_micro=_ACTUAL, price_book_version="2026-06-22")

    ctx = uow._ctx
    assert ctx.counter_store.ensured == [
        (BudgetId("b-org"), _PERIOD),
        (BudgetId("b-user"), _PERIOD),
    ]
    assert ctx.counter_store.apply_calls == [
        (BudgetId("b-org"), _PERIOD, _ACTUAL),
        (BudgetId("b-user"), _PERIOD, _ACTUAL),
    ]
    # never denies: no reserve call happens (the fake would raise if it were called)
    assert [e.kind for e in ctx.ledger.appended] == [LedgerKind.METER] * 2
    by_budget = {e.budget_id: e for e in ctx.ledger.appended}
    assert by_budget[BudgetId("b-org")].delta_committed_micro == _ACTUAL
    assert by_budget[BudgetId("b-org")].delta_overage_micro == 0
    assert by_budget[BudgetId("b-user")].delta_committed_micro == 150
    assert by_budget[BudgetId("b-user")].delta_overage_micro == 40
    assert all(e.provider == "anthropic" for e in ctx.ledger.appended)
    assert all(e.model == "claude" for e in ctx.ledger.appended)
    assert all(dict(e.labels or {}) == _LABELS for e in ctx.ledger.appended)
    assert all(e.price_book_version == "2026-06-22" for e in ctx.ledger.appended)
    assert all(e.actual_input_tokens == 100 for e in ctx.ledger.appended)
    assert all(e.actual_output_tokens == 50 for e in ctx.ledger.appended)
    assert ctx.metered_receipt.stored == [
        (
            "u1",
            "idem-meter",
            {"actual_micro": _ACTUAL, "price_book_version": "2026-06-22"},
        )
    ]
    assert uow.committed is True


async def test_meter_marks_truncated_calls_on_every_ledger_row() -> None:
    handler, uow = _build()
    await handler.meter(_auth(), _command(truncated=True))
    assert uow._ctx.ledger.appended
    assert all(e.ref == "truncated" for e in uow._ctx.ledger.appended)


async def test_meter_defaults_to_no_ref_when_not_truncated() -> None:
    handler, uow = _build()
    await handler.meter(_auth(), _command())
    assert uow._ctx.ledger.appended
    assert all(e.ref is None for e in uow._ctx.ledger.appended)


async def test_meter_prices_all_four_token_classes() -> None:
    usage = ProviderUsage(
        input_tokens=100, output_tokens=50, cached_input_tokens=20, cache_creation_tokens=8
    )
    expected = actual_micro(
        _PRICE,
        input_tokens=100,
        output_tokens=50,
        cached_input_tokens=20,
        cache_creation_tokens=8,
    )
    # sanity: the creation term actually moves the total versus the no-creation case
    assert expected == _ACTUAL + 10  # 8 tokens * 1.25 = 10
    handler, _uow = _build()
    result = await handler.meter(_auth(), _command(usage=usage))
    assert result.actual_micro == expected


async def test_meter_with_empty_applicable_set_is_rejected() -> None:
    handler, uow = _build(ancestry=())
    with pytest.raises(BudgetNotFound):
        await handler.meter(_auth(), _command())
    assert uow.rolled_back is True


async def test_meter_denies_an_unknown_model_and_rolls_back() -> None:
    handler, uow = _build(priced=None)
    with pytest.raises(UnknownModel):
        await handler.meter(_auth(), _command())
    assert uow.rolled_back is True


async def test_meter_rejects_a_key_reused_with_a_different_command() -> None:
    handler, uow = _build(idem_outcome=ClaimOutcome.MISMATCH)
    with pytest.raises(IdempotencyKeyReuse):
        await handler.meter(_auth(), _command())
    assert uow.rolled_back is True


async def test_meter_replays_a_stored_response_without_reapplying() -> None:
    stored = {"actual_micro": 190, "price_book_version": "2026-06-01"}
    handler, uow = _build(idem_outcome=ClaimOutcome.REPLAY, idem_response=stored)
    result = await handler.meter(_auth(), _command())
    assert result == MeterResult(actual_micro=190, price_book_version="2026-06-01")
    assert uow._ctx.counter_store.apply_calls == []
    assert uow._ctx.ledger.appended == []
    assert uow.committed is True


async def test_meter_includes_an_authorized_project_budget() -> None:
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE, _ORG_NODE), project=project)
    await handler.meter(_auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("proj-1")))
    applied_budgets = [call[0] for call in uow._ctx.counter_store.apply_calls]
    assert BudgetId("b-proj") in applied_budgets


async def test_meter_rejects_a_project_outside_the_credential_scope() -> None:
    # mirrors test_grace.py::test_backfill_rejects_a_project_outside_the_credential_scope:
    # the project resolves, but the credential's own ancestry doesn't reach its org, so it is
    # denied identically to an unknown project (never revealing which) and rolls back nothing.
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE,), project=project)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.meter(_auth(ScopeKind.USER, "u1"), _command(project_id=ProjectId("proj-1")))
    assert excinfo.value.scope == "project:proj-1"
    assert uow.rolled_back is True
    assert uow._ctx.counter_store.apply_calls == []
    assert uow._ctx.ledger.appended == []
    assert uow._ctx.metered_receipt.stored == []


def test_meter_fingerprint_is_stable_and_command_sensitive() -> None:
    principal = _principal()
    base = _command()
    assert meter_fingerprint(principal, base) == meter_fingerprint(principal, base)
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal, _command(provider="openai")
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(principal, _command(model="gpt"))
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal,
        _command(usage=ProviderUsage(input_tokens=101, output_tokens=50, cached_input_tokens=20)),
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal,
        _command(usage=ProviderUsage(input_tokens=100, output_tokens=51, cached_input_tokens=20)),
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal,
        _command(usage=ProviderUsage(input_tokens=100, output_tokens=50, cached_input_tokens=21)),
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal,
        _command(
            usage=ProviderUsage(
                input_tokens=100, output_tokens=50, cached_input_tokens=20, cache_creation_tokens=10
            )
        ),
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal, _command(project_id=ProjectId("proj-1"))
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal, _command(labels={"team": "platform"})
    )
    assert meter_fingerprint(principal, base) != meter_fingerprint(
        principal, _command(truncated=True)
    )


def test_meter_fingerprint_is_independent_of_label_order() -> None:
    principal = _principal()
    forward = _command(labels={"a": "1", "b": "2"})
    backward = _command(labels={"b": "2", "a": "1"})
    assert meter_fingerprint(principal, forward) == meter_fingerprint(principal, backward)
