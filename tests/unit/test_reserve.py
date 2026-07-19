"""Unit tests for the reserve handler and its transaction envelope (§4, §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.reserve import ReserveHandler, reserve_fingerprint
from tollgate.domain.commands import ReserveCommand, ReserveResult
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import (
    BudgetNotFound,
    IdempotencyKeyReuse,
    InsufficientBudget,
    NonPositiveEstimate,
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
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ReserveOutcome, ResolvedProject, ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
_PRICE = ModelPrice(
    provider="anthropic",
    model="claude",
    input_micro_per_token=Decimal("1"),
    output_micro_per_token=Decimal("2"),
    cached_input_micro_per_token=Decimal("0.5"),
)
_PRICED = PricedModel(version="2026-06-22", price=_PRICE)
# estimate = 1*100 + 2*100 = 300
_ESTIMATE = 300

_USER_NODE = BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1")
_ORG_NODE = BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")
_PROJECT_NODE = BudgetNode(BudgetId("b-proj"), ScopeKind.PROJECT, "proj-1")


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


def _command(**overrides: Any) -> ReserveCommand:
    base = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude",
        input_bound_tokens=100,
        max_output_tokens=100,
        labels={"env": "prod"},
        project_id=None,
    )
    return replace(base, **overrides)


class _FakeIdempotency:
    def __init__(
        self, outcome: ClaimOutcome = ClaimOutcome.FRESH, response: Mapping[str, Any] | None = None
    ) -> None:
        self._claim = IdempotencyClaim(outcome, response=response)
        self.claimed: tuple[str, str, str] | None = None
        self.stored: list[tuple[str, str, str, dict[str, Any]]] = []

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        self.claimed = (principal_id, key, fingerprint)
        return self._claim

    async def store_response(
        self, principal_id: str, key: str, status: str, response: Mapping[str, Any]
    ) -> None:
        self.stored.append((principal_id, key, status, dict(response)))

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        raise AssertionError("this handler never reaps keys")


class _FakeReservations:
    def __init__(self) -> None:
        self.inserted: tuple[ReservationRecord, list[ReservationLineRecord]] | None = None

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        self.inserted = (reservation, list(lines))

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        return True

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


class _FakeLedger:
    def __init__(self) -> None:
        self.appended: list[LedgerEntry] | None = None

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        self.appended = list(entries)


class _StubCounterStore:
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
        return None

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        return Reconciliation(committed_micro=amount_micro, overage_micro=0)


class _StubReserveTx:
    def __init__(self, outcome: ReserveOutcome) -> None:
        self._outcome = outcome
        self.calls: list[tuple[list[BudgetNode], datetime, int]] = []

    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> ReserveOutcome:
        self.calls.append((list(nodes), period_start, amount_micro))
        return self._outcome


class _StubPrices:
    def __init__(self, priced: PricedModel | None) -> None:
        self._priced = priced

    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return self._priced

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        return None


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


class _Ctx:
    def __init__(
        self,
        *,
        prices: _StubPrices,
        budgets: _StubBudgets,
        idempotency: _FakeIdempotency,
        reservations: _FakeReservations,
        ledger: _FakeLedger,
        reserve_tx: _StubReserveTx,
    ) -> None:
        self.prices = prices
        self.budgets = budgets
        self.idempotency = idempotency
        self.reservations = reservations
        self.ledger = ledger
        self.reserve_tx = reserve_tx
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


class _SeqIds:
    def __init__(self) -> None:
        self._n = 0

    def new_reservation_id(self) -> ReservationId:
        return ReservationId("res-1")

    def new_ledger_entry_id(self) -> LedgerEntryId:
        self._n += 1
        return LedgerEntryId(f"led-{self._n}")


def _build(
    *,
    prices: PricedModel | None = _PRICED,
    ancestry: Sequence[BudgetNode] = (_USER_NODE, _ORG_NODE),
    project: ResolvedProject | None = None,
    reserve_outcome: ReserveOutcome = ReserveOutcome(ok=True),  # noqa: B008
    idem_outcome: ClaimOutcome = ClaimOutcome.FRESH,
    idem_response: Mapping[str, Any] | None = None,
) -> tuple[ReserveHandler, _Uow]:
    ctx = _Ctx(
        prices=_StubPrices(prices),
        budgets=_StubBudgets(ancestry, project),
        idempotency=_FakeIdempotency(idem_outcome, idem_response),
        reservations=_FakeReservations(),
        ledger=_FakeLedger(),
        reserve_tx=_StubReserveTx(reserve_outcome),
    )
    uow = _Uow(ctx)
    handler = ReserveHandler(
        uow=uow, clock=_FixedClock(), ids=_SeqIds(), reservation_ttl_seconds=600
    )
    return handler, uow


async def test_reserve_succeeds_and_persists_the_envelope() -> None:
    handler, uow = _build()
    result = await handler.reserve(_auth(), _command())
    assert result == ReserveResult(
        reservation_id=ReservationId("res-1"),
        estimated_micro=_ESTIMATE,
        price_book_version="2026-06-22",
        ttl_deadline=_NOW + timedelta(seconds=600),
    )
    ctx = uow._ctx
    # the guarded reserve saw the full applicable set, lock-ordered org < user (§5.3)
    assert ctx.reserve_tx.calls == [([_ORG_NODE, _USER_NODE], _PERIOD, _ESTIMATE)]
    assert ctx.reservations.inserted is not None
    record, lines = ctx.reservations.inserted
    assert record.estimated_micro == _ESTIMATE
    assert record.price_book_version == "2026-06-22"
    assert record.principal_id == "u1"
    assert {line.budget_id for line in lines} == {"b-user", "b-org"}
    assert all(line.amount_micro == _ESTIMATE and line.period_start == _PERIOD for line in lines)
    assert ctx.ledger.appended is not None
    assert len(ctx.ledger.appended) == 2
    assert all(e.delta_reserved_micro == _ESTIMATE for e in ctx.ledger.appended)
    assert ctx.idempotency.stored == [
        (
            "u1",
            "idem-1",
            "succeeded",
            {
                "reservation_id": "res-1",
                "estimated_micro": _ESTIMATE,
                "price_book_version": "2026-06-22",
                "ttl_deadline": (_NOW + timedelta(seconds=600)).isoformat(),
            },
        )
    ]
    assert uow.committed is True
    # the acting principal scopes the idempotency claim and the cached response (#71)
    assert ctx.idempotency.claimed == (
        "u1",
        "idem-1",
        reserve_fingerprint(_principal(), _command()),
    )


async def test_reserve_replays_a_stored_response_without_re_reserving() -> None:
    stored = {
        "reservation_id": "res-prev",
        "estimated_micro": 42,
        "price_book_version": "2026-06-01",
        "ttl_deadline": _NOW.isoformat(),
    }
    handler, uow = _build(idem_outcome=ClaimOutcome.REPLAY, idem_response=stored)
    result = await handler.reserve(_auth(), _command())
    assert result == ReserveResult(
        reservation_id=ReservationId("res-prev"),
        estimated_micro=42,
        price_book_version="2026-06-01",
        ttl_deadline=_NOW,
    )
    assert uow._ctx.reserve_tx.calls == []  # no new reserve on replay
    assert uow._ctx.reservations.inserted is None
    assert uow.committed is True


async def test_reserve_rejects_a_key_reused_with_a_different_command() -> None:
    handler, uow = _build(idem_outcome=ClaimOutcome.MISMATCH)
    with pytest.raises(IdempotencyKeyReuse):
        await handler.reserve(_auth(), _command())
    assert uow.rolled_back is True


async def test_reserve_denies_an_unknown_model_and_rolls_back() -> None:
    handler, uow = _build(prices=None)
    with pytest.raises(UnknownModel):
        await handler.reserve(_auth(), _command())
    assert uow.rolled_back is True


async def test_reserve_denies_an_empty_applicable_set() -> None:
    handler, uow = _build(ancestry=())
    with pytest.raises(BudgetNotFound):
        await handler.reserve(_auth(), _command())
    assert uow.rolled_back is True


async def test_reserve_denies_insufficient_budget_naming_the_binding_node() -> None:
    handler, uow = _build(reserve_outcome=ReserveOutcome(ok=False, binding_node=_USER_NODE))
    with pytest.raises(InsufficientBudget) as excinfo:
        await handler.reserve(_auth(), _command())
    assert excinfo.value.scope == "user:u1"
    assert uow._ctx.reservations.inserted is None  # nothing persisted
    assert uow.rolled_back is True


async def test_reserve_denies_a_zero_worst_case_estimate() -> None:
    # A reserve whose worst-case estimate is zero gates nothing; deny it before touching
    # any balance, and roll back so no idempotency key is cached (#65).
    handler, uow = _build()
    with pytest.raises(NonPositiveEstimate):
        await handler.reserve(_auth(), _command(input_bound_tokens=0, max_output_tokens=0))
    assert uow._ctx.reserve_tx.calls == []  # never reached the balance guard
    assert uow._ctx.reservations.inserted is None
    assert uow._ctx.idempotency.stored == []
    assert uow.rolled_back is True


async def test_reserve_includes_an_authorized_project_budget() -> None:
    # an org-scoped credential authorizes a project under its org
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE, _ORG_NODE), project=project)
    await handler.reserve(_auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("proj-1")))
    (nodes, _period, _amount) = uow._ctx.reserve_tx.calls[0]
    assert _PROJECT_NODE in nodes


async def test_reserve_rejects_a_project_outside_the_credential_scope() -> None:
    # a user-scoped credential cannot name a project (a project has no user ancestor)
    project = ResolvedProject(org_id=OrgId("o1"), budget=_PROJECT_NODE)
    handler, uow = _build(ancestry=(_USER_NODE,), project=project)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.reserve(_auth(ScopeKind.USER, "u1"), _command(project_id=ProjectId("proj-1")))
    assert excinfo.value.scope == "project:proj-1"
    assert uow.rolled_back is True


async def test_reserve_rejects_an_unknown_project_without_revealing_it() -> None:
    handler, uow = _build(ancestry=(_USER_NODE,), project=None)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        await handler.reserve(_auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("ghost")))
    assert excinfo.value.scope == "project:ghost"
    assert uow.rolled_back is True


def test_reserve_fingerprint_is_stable_and_command_sensitive() -> None:
    principal = _principal()
    base = _command()
    assert reserve_fingerprint(principal, base) == reserve_fingerprint(principal, base)
    assert reserve_fingerprint(principal, base) != reserve_fingerprint(
        principal, replace(base, max_output_tokens=200)
    )
    # label order does not change the fingerprint
    reordered = replace(base, labels={"env": "prod", "team": "blue"})
    other_order = replace(base, labels={"team": "blue", "env": "prod"})
    assert reserve_fingerprint(principal, reordered) == reserve_fingerprint(principal, other_order)
