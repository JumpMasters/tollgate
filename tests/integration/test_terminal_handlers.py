"""End-to-end lifecycle against real Postgres: commit/cancel/extend, self-heal, grace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.schema import budget, org, price, price_book, team, user_principal
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.auth import AuthContext
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.reap import IdempotencyReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import (
    CancelCommand,
    CommitCommand,
    CommitResult,
    ExtendCommand,
    GraceBackfillCommand,
    ProviderUsage,
    ReserveCommand,
    ReserveResult,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import BudgetNotFound, ReservationNotHeld
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


class _ClockAt:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def _seed_prices(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(price_book.insert().values(version="pb-1", published_at=_PERIOD))
        await conn.execute(
            price.insert().values(
                price_book_version="pb-1",
                provider="anthropic",
                model="claude",
                input_micro_per_token=Decimal("1"),
                output_micro_per_token=Decimal("2"),
                cached_input_micro_per_token=Decimal("0.5"),
                cache_creation_micro_per_token=Decimal("1.25"),
            )
        )


async def _seed_identity(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
        await conn.execute(
            user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
        )


async def _seed_budgets(engine: AsyncEngine, *, user_limit: int, org_limit: int = 1_000) -> None:
    async with engine.begin() as conn:
        for budget_id, scope_kind, scope_id, limit in (
            ("b-org", "org", "o1", org_limit),
            ("b-user", "user", "u1", user_limit),
        ):
            await conn.execute(
                budget.insert().values(
                    budget_id=budget_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    period_kind="calendar_month",
                    hard_limit_micro=limit,
                )
            )


async def _seed(engine: AsyncEngine, *, user_limit: int = 1_000, org_limit: int = 1_000) -> None:
    await _seed_prices(engine)
    await _seed_identity(engine)
    await _seed_budgets(engine, user_limit=user_limit, org_limit=org_limit)


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


async def _reserve(engine: AsyncEngine, key: str = "idem-res") -> ReserveResult:
    handler = ReserveHandler(
        uow=PostgresUnitOfWork(engine),
        clock=_ClockAt(_NOW),
        ids=Uuid7IdGenerator(),
        reservation_ttl_seconds=600,
    )
    command = ReserveCommand(
        idempotency_key=key,
        provider="anthropic",
        model="claude",
        input_bound_tokens=100,
        max_output_tokens=100,
        labels={"env": "prod"},
    )  # estimate = 1*100 + 2*100 = 300
    return await handler.reserve(_auth(), command)


def _commit_handler(engine: AsyncEngine) -> CommitHandler:
    return CommitHandler(uow=PostgresUnitOfWork(engine), ids=Uuid7IdGenerator())


def _cancel_handler(engine: AsyncEngine) -> CancelHandler:
    return CancelHandler(uow=PostgresUnitOfWork(engine), ids=Uuid7IdGenerator())


def _extend_handler(engine: AsyncEngine, clock: _ClockAt) -> ExtendHandler:
    return ExtendHandler(uow=PostgresUnitOfWork(engine), clock=clock, reservation_ttl_seconds=600)


def _grace_handler(engine: AsyncEngine) -> GraceBackfillHandler:
    return GraceBackfillHandler(
        uow=PostgresUnitOfWork(engine), clock=_ClockAt(_NOW), ids=Uuid7IdGenerator()
    )


async def _scalar(engine: AsyncEngine, sql: str, params: dict[str, Any] | None = None) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _balances(engine: AsyncEngine, budget_id: str) -> tuple[int, int, int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT reserved_micro, committed_micro, overage_micro"
                    " FROM budget_balance WHERE budget_id = :b"
                ),
                {"b": budget_id},
            )
        ).one()
    return (row.reserved_micro, row.committed_micro, row.overage_micro)


async def _emulate_reap(engine: AsyncEngine, reservation_id: str, estimate: int) -> None:
    """Apply the reaper's effect directly (the worker itself is exercised end-to-end in
    test_reservation_reaper.py): settle the status and release the held estimate on every
    balance."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE reservation SET status = 'reaped' WHERE reservation_id = :r"),
            {"r": reservation_id},
        )
        await conn.execute(
            text(
                "UPDATE budget_balance SET reserved_micro = reserved_micro - :est"
                " WHERE (budget_id, period_start) IN ("
                "SELECT budget_id, period_start FROM reservation_line"
                " WHERE reservation_id = :r)"
            ),
            {"est": estimate, "r": reservation_id},
        )


async def test_commit_reconciles_and_persists_the_envelope(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    result = await _commit_handler(committing_engine).commit(
        _auth(),
        CommitCommand(
            idempotency_key="idem-commit",
            reservation_id=reserved.reservation_id,
            usage=ProviderUsage(input_tokens=100, output_tokens=50),
        ),
    )  # actual = 100*1 + 50*2 = 200
    assert result == CommitResult(
        reservation_id=reserved.reservation_id, committed_micro=200, overage_micro=0
    )
    assert await _balances(committing_engine, "b-user") == (0, 200, 0)
    assert await _balances(committing_engine, "b-org") == (0, 200, 0)
    status = await _scalar(
        committing_engine,
        "SELECT status FROM reservation WHERE reservation_id = :r",
        {"r": reserved.reservation_id},
    )
    assert status == "committed"
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='commit_adjust'")
        == 2
    )
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='overage'") == 0
    assert (
        await _scalar(
            committing_engine, "SELECT count(*) FROM idempotency_key WHERE response IS NOT NULL"
        )
        == 1
    )  # the reserve (commit's response now lives on the durable metered_receipt, #125)
    assert (
        await _scalar(
            committing_engine, "SELECT count(*) FROM metered_receipt WHERE response IS NOT NULL"
        )
        == 1
    )  # the commit's durable receipt (#125)


async def test_commit_records_overage_above_the_estimate(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    result = await _commit_handler(committing_engine).commit(
        _auth(),
        CommitCommand(
            idempotency_key="idem-commit",
            reservation_id=reserved.reservation_id,
            usage=ProviderUsage(input_tokens=300, output_tokens=100),
        ),
    )  # actual = 300 + 200 = 500 > est 300
    assert result == CommitResult(
        reservation_id=reserved.reservation_id, committed_micro=300, overage_micro=200
    )
    assert await _balances(committing_engine, "b-user") == (0, 300, 200)
    assert await _balances(committing_engine, "b-org") == (0, 300, 200)
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='overage'") == 2


async def test_duplicate_commit_replays_without_double_applying(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    handler = _commit_handler(committing_engine)
    command = CommitCommand(
        idempotency_key="idem-commit",
        reservation_id=reserved.reservation_id,
        usage=ProviderUsage(input_tokens=100, output_tokens=50),
    )
    first = await handler.commit(_auth(), command)
    second = await handler.commit(_auth(), command)
    assert second == first
    assert await _balances(committing_engine, "b-user") == (0, 200, 0)
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='commit_adjust'")
        == 2
    )


async def test_commit_retry_after_key_ttl_replays_via_durable_receipt(
    committing_engine: AsyncEngine,
) -> None:
    # #125: commit's response cache lives on the never-reaped metered_receipt table, not the
    # TTL'd idempotency_key. A retry after the key's TTL must replay the original result instead
    # of re-claiming fresh and failing the settled-status guard with an ambiguous
    # ReservationNotHeld. (Pre-fix this raised ReservationNotHeld; verified red-then-green.)
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    command = CommitCommand(
        idempotency_key="idem-commit",
        reservation_id=reserved.reservation_id,
        usage=ProviderUsage(input_tokens=100, output_tokens=50),
    )  # actual = 100*1 + 50*2 = 200
    first = await _commit_handler(committing_engine).commit(_auth(), command)
    assert first == CommitResult(
        reservation_id=reserved.reservation_id, committed_micro=200, overage_micro=0
    )

    # Age well past the idempotency-key TTL and drain the reaped table. A clock far in the future
    # guarantees the cutoff is past every key's real created_at, so the reaper WOULD have deleted
    # the commit key had it been stored there.
    reaper = IdempotencyReaperHandler(
        uow=PostgresUnitOfWork(committing_engine),
        clock=_ClockAt(datetime.now(UTC) + timedelta(days=2)),
        ttl_hours=24,
        batch_size=100,
        max_batches_per_tick=100,
    )
    deleted = await reaper.run_once()
    assert deleted >= 1  # the aged idempotency_key rows (the reserve key) were reaped
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0

    # The retry replays the original result (does NOT raise ReservationNotHeld, the pre-fix
    # symptom) and re-applies nothing.
    replay = await _commit_handler(committing_engine).commit(_auth(), command)
    assert replay == first
    assert await _balances(committing_engine, "b-user") == (0, 200, 0)
    assert await _balances(committing_engine, "b-org") == (0, 200, 0)
    status = await _scalar(
        committing_engine,
        "SELECT status FROM reservation WHERE reservation_id = :r",
        {"r": reserved.reservation_id},
    )
    assert status == "committed"
    # the replay came from the durable receipt, not the reaped idempotency table
    assert (
        await _scalar(
            committing_engine,
            "SELECT count(*) FROM metered_receipt WHERE response IS NOT NULL",
        )
        == 1
    )


async def test_commit_after_cancel_is_rejected_and_rolls_back(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    await _cancel_handler(committing_engine).cancel(
        _auth(),
        CancelCommand(idempotency_key="idem-cancel", reservation_id=reserved.reservation_id),
    )
    with pytest.raises(ReservationNotHeld):
        await _commit_handler(committing_engine).commit(
            _auth(),
            CommitCommand(
                idempotency_key="idem-late",
                reservation_id=reserved.reservation_id,
                usage=ProviderUsage(input_tokens=100, output_tokens=50),
            ),
        )
    # the rejected commit persisted nothing: no receipt, no balance change. The reserve key is on
    # idempotency_key and the cancel's receipt on the durable metered_receipt (#125); the rolled-
    # back late commit left metered_receipt at just the cancel's one row.
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 1  # reserve
    assert await _scalar(committing_engine, "SELECT count(*) FROM metered_receipt") == 1  # cancel
    assert await _balances(committing_engine, "b-user") == (0, 0, 0)


async def test_late_commit_self_heals_a_reaped_reservation(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine, user_limit=400)
    first = await _reserve(committing_engine, key="idem-1")  # holds 300
    await _emulate_reap(committing_engine, first.reservation_id, estimate=300)
    second = await _reserve(committing_engine, key="idem-2")  # re-takes the freed headroom
    assert second.reservation_id != first.reservation_id

    result = await _commit_handler(committing_engine).commit(
        _auth(),
        CommitCommand(
            idempotency_key="idem-heal",
            reservation_id=first.reservation_id,
            usage=ProviderUsage(input_tokens=100, output_tokens=50),
        ),
    )  # actual 200; user remaining = 400-300 = 100 -> (100, 100); org remaining 700 -> (200, 0)
    assert result == CommitResult(
        reservation_id=first.reservation_id, committed_micro=100, overage_micro=100
    )
    assert await _balances(committing_engine, "b-user") == (300, 100, 100)
    assert await _balances(committing_engine, "b-org") == (300, 200, 0)
    status = await _scalar(
        committing_engine,
        "SELECT status FROM reservation WHERE reservation_id = :r",
        {"r": first.reservation_id},
    )
    assert status == "committed"
    assert (
        await _scalar(
            committing_engine,
            "SELECT count(*) FROM ledger WHERE kind='commit_adjust' AND ref='late_commit'",
        )
        == 2
    )
    assert (
        await _scalar(
            committing_engine,
            "SELECT count(*) FROM ledger WHERE kind='overage' AND ref='late_commit'",
        )
        == 1
    )


async def test_cancel_releases_the_reservation(committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    handler = _cancel_handler(committing_engine)
    command = CancelCommand(idempotency_key="idem-cancel", reservation_id=reserved.reservation_id)
    result = await handler.cancel(_auth(), command)
    assert result.released_micro == 300
    assert await _balances(committing_engine, "b-user") == (0, 0, 0)
    assert await _balances(committing_engine, "b-org") == (0, 0, 0)
    status = await _scalar(
        committing_engine,
        "SELECT status FROM reservation WHERE reservation_id = :r",
        {"r": reserved.reservation_id},
    )
    assert status == "released"
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='release'") == 2
    replay = await handler.cancel(_auth(), command)
    assert replay == result
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='release'") == 2


async def test_extend_advances_the_ttl_monotonically(committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    assert reserved.ttl_deadline == _NOW + timedelta(seconds=600)
    later = await _extend_handler(
        committing_engine, _ClockAt(_NOW + timedelta(seconds=300))
    ).extend(_auth(), ExtendCommand(reservation_id=reserved.reservation_id))
    assert later.ttl_deadline == _NOW + timedelta(seconds=900)
    # a stale heartbeat (older clock) never moves the deadline backward
    stale = await _extend_handler(committing_engine, _ClockAt(_NOW)).extend(
        _auth(), ExtendCommand(reservation_id=reserved.reservation_id)
    )
    assert stale.ttl_deadline == _NOW + timedelta(seconds=900)


async def test_extend_after_cancel_is_rejected(committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    await _cancel_handler(committing_engine).cancel(
        _auth(),
        CancelCommand(idempotency_key="idem-cancel", reservation_id=reserved.reservation_id),
    )
    with pytest.raises(ReservationNotHeld):
        await _extend_handler(committing_engine, _ClockAt(_NOW)).extend(
            _auth(), ExtendCommand(reservation_id=reserved.reservation_id)
        )


async def test_grace_backfill_records_spend_and_overage(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine, user_limit=50)
    handler = _grace_handler(committing_engine)
    command = GraceBackfillCommand(
        idempotency_key="idem-grace",
        provider="anthropic",
        model="claude",
        usage=ProviderUsage(input_tokens=100, output_tokens=50),
    )  # actual 200; user remaining 50 -> (50, 150); org remaining 1000 -> (200, 0)
    result = await handler.backfill(_auth(), command)
    assert result.actual_micro == 200
    assert result.price_book_version == "pb-1"
    assert await _balances(committing_engine, "b-user") == (0, 50, 150)
    assert await _balances(committing_engine, "b-org") == (0, 200, 0)
    assert (
        await _scalar(
            committing_engine,
            "SELECT count(*) FROM ledger WHERE kind='grace_backfill' AND reservation_id IS NULL",
        )
        == 2
    )
    replay = await handler.backfill(_auth(), command)
    assert replay == result
    assert await _balances(committing_engine, "b-user") == (0, 50, 150)


async def test_grace_backfill_with_no_governing_budget_is_rejected(
    committing_engine: AsyncEngine,
) -> None:
    await _seed_prices(committing_engine)
    await _seed_identity(committing_engine)  # no budgets anywhere
    with pytest.raises(BudgetNotFound):
        await _grace_handler(committing_engine).backfill(
            _auth(),
            GraceBackfillCommand(
                idempotency_key="idem-grace",
                provider="anthropic",
                model="claude",
                usage=ProviderUsage(input_tokens=100, output_tokens=50),
            ),
        )
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger") == 0
