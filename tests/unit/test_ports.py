"""Tests that conforming fakes satisfy the application ports.

The ports are structural (Protocols); these both document the expected shape and let mypy
verify a concrete implementation conforms.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from tollgate.application.ports import (
    BudgetRepository,
    Clock,
    CommandContext,
    CounterStore,
    CredentialRepository,
    IdempotencyRepository,
    IdGenerator,
    LedgerRepository,
    PriceBookRepository,
    ReservationRepository,
    ReserveTransaction,
    UnitOfWork,
)
from tollgate.domain.credentials import Credential, Principal
from tollgate.domain.ids import (
    BudgetId,
    LedgerEntryId,
    OrgId,
    PrincipalId,
    ProjectId,
    ReservationId,
    TeamId,
    UserId,
)
from tollgate.domain.pricing import PricedModel
from tollgate.domain.records import (
    ClaimOutcome,
    IdempotencyClaim,
    LedgerEntry,
    LedgerKind,
    ReservationLineRecord,
    ReservationRecord,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ReserveOutcome, ResolvedProject, ScopeKind

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


class _FakeReserveTransaction:
    async def reserve(
        self, nodes: Sequence[BudgetNode], period_start: datetime, amount_micro: int
    ) -> ReserveOutcome:
        return ReserveOutcome(ok=True)


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

    # The structural conformance check is the Protocol-typed assignment above
    # (mypy --strict verifies it); the calls below only smoke that nothing throws.
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


async def test_fake_conforms_to_reserve_transaction() -> None:
    gate: ReserveTransaction = _FakeReserveTransaction()
    outcome = await gate.reserve([BudgetNode(BudgetId("b1"), ScopeKind.ORG, "o1")], _PERIOD, 100)
    assert outcome.ok is True
    assert outcome.binding_node is None


class _FakeCredentialRepository:
    async def find_by_token_hash(self, token_hash: str) -> Credential | None:
        return None

    async def load_principal(self, principal_id: PrincipalId) -> Principal | None:
        return None


async def test_fake_conforms_to_credential_repository() -> None:
    repo: CredentialRepository = _FakeCredentialRepository()
    assert await repo.find_by_token_hash("hash") is None
    assert await repo.load_principal(PrincipalId("u1")) is None


def _principal() -> Principal:
    return Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))


class _FakeClock:
    def now(self) -> datetime:
        return _PERIOD


class _FakeIds:
    def new_reservation_id(self) -> ReservationId:
        return ReservationId("r1")

    def new_ledger_entry_id(self) -> LedgerEntryId:
        return LedgerEntryId("e1")


class _FakePriceBook:
    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        return None


class _FakeBudgets:
    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        return ()

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        return None


class _FakeCommandContext:
    def __init__(self) -> None:
        self.prices: PriceBookRepository = _FakePriceBook()
        self.budgets: BudgetRepository = _FakeBudgets()
        self.idempotency: IdempotencyRepository = _FakeIdempotencyRepository()
        self.reservations: ReservationRepository = _FakeReservationRepository()
        self.ledger: LedgerRepository = _FakeLedgerRepository()
        self.reserve_tx: ReserveTransaction = _FakeReserveTransaction()


class _FakeUnitOfWork:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[CommandContext]:
        yield _FakeCommandContext()


def test_fakes_conform_to_clock_and_id_generator() -> None:
    clock: Clock = _FakeClock()
    ids: IdGenerator = _FakeIds()
    assert clock.now() == _PERIOD
    assert ids.new_reservation_id() == "r1"
    assert ids.new_ledger_entry_id() == "e1"


async def test_fakes_conform_to_price_and_budget_repositories() -> None:
    prices: PriceBookRepository = _FakePriceBook()
    budgets: BudgetRepository = _FakeBudgets()
    assert await prices.resolve_price("anthropic", "claude") is None
    assert await budgets.find_ancestry_budgets(_principal()) == ()
    assert await budgets.find_project(ProjectId("p1")) is None


async def test_fake_conforms_to_unit_of_work() -> None:
    uow: UnitOfWork = _FakeUnitOfWork()
    async with uow.begin() as ctx:
        context: CommandContext = ctx
        assert await context.prices.resolve_price("a", "b") is None
        assert await context.budgets.find_ancestry_budgets(_principal()) == ()
        outcome = await context.reserve_tx.reserve(
            [BudgetNode(BudgetId("b1"), ScopeKind.ORG, "o1")], _PERIOD, 1
        )
        assert outcome.ok is True
