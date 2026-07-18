"""Reservation reaper end-to-end against real Postgres: release, heartbeat, SKIP LOCKED,
self-heal exactly-once (§5.4, §5.5, §7.2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.schema import budget, org, price, price_book, team, user_principal
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.auth import AuthContext
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.reap import ReapReport, ReservationReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import (
    CommitCommand,
    CommitResult,
    ProviderUsage,
    ReserveCommand,
    ReserveResult,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
_AFTER_TTL = _NOW + timedelta(seconds=601)  # reserve sets ttl_deadline = _NOW + 600s


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


def _reaper(engine: AsyncEngine, clock: _ClockAt) -> ReservationReaperHandler:
    return ReservationReaperHandler(
        uow=PostgresUnitOfWork(engine), clock=clock, ids=Uuid7IdGenerator(), batch_size=100
    )


async def test_reaper_releases_an_abandoned_reservation(committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine)
    reserved = await _reserve(committing_engine)
    assert await _balances(committing_engine, "b-user") == (300, 0, 0)

    report = await _reaper(committing_engine, _ClockAt(_AFTER_TTL)).run_once()
    assert report == ReapReport(reaped=1)

    assert await _balances(committing_engine, "b-user") == (0, 0, 0)  # estimate released
    assert await _balances(committing_engine, "b-org") == (0, 0, 0)
    status = await _scalar(
        committing_engine,
        "SELECT status FROM reservation WHERE reservation_id = :r",
        {"r": reserved.reservation_id},
    )
    assert status == "reaped"
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='reap'") == 2


async def test_reaper_spares_a_reservation_that_has_not_expired(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine)
    await _reserve(committing_engine)
    # clock before the ttl_deadline: nothing is expired
    report = await _reaper(committing_engine, _ClockAt(_NOW + timedelta(seconds=1))).run_once()
    assert report == ReapReport(reaped=0)
    assert await _balances(committing_engine, "b-user") == (300, 0, 0)  # still held
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='reap'") == 0


async def test_concurrent_reapers_never_double_reap(committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine, user_limit=100_000, org_limit=100_000)
    for i in range(6):
        await _reserve(committing_engine, key=f"idem-{i}")  # six held reservations
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-user'"
        )
        == 1800
    )

    left = _reaper(committing_engine, _ClockAt(_AFTER_TTL))
    right = _reaper(committing_engine, _ClockAt(_AFTER_TTL))
    reports = await asyncio.gather(left.run_once(), right.run_once())

    assert sum(r.reaped for r in reports) == 6  # each reservation reaped exactly once
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-user'"
        )
        == 0
    )
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='reap'") == 12
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM reservation WHERE status='held'")
        == 0
    )


async def test_real_reap_then_commit_self_heals_exactly_once(
    committing_engine: AsyncEngine,
) -> None:
    await _seed(committing_engine, user_limit=400)
    first = await _reserve(committing_engine, key="idem-1")  # holds 300

    report = await _reaper(committing_engine, _ClockAt(_AFTER_TTL)).run_once()
    assert report == ReapReport(reaped=1)
    assert await _balances(committing_engine, "b-user") == (0, 0, 0)  # real reaper freed the hold

    second = await _reserve(committing_engine, key="idem-2")  # re-takes the freed headroom
    assert second.reservation_id != first.reservation_id

    result = await CommitHandler(
        uow=PostgresUnitOfWork(committing_engine), ids=Uuid7IdGenerator()
    ).commit(
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
    assert status == "committed"  # reaped -> committed, exactly one terminal effect
    # the reaper's release rows and the late commit's adjust rows coexist in the ledger
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='reap'") == 2
    assert (
        await _scalar(
            committing_engine,
            "SELECT count(*) FROM ledger WHERE kind='commit_adjust' AND ref='late_commit'",
        )
        == 2
    )
