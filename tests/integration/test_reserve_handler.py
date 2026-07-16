"""End-to-end reserve against real Postgres: the §5 envelope under real commits (§4, §5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.schema import (
    budget,
    org,
    price,
    price_book,
    project,
    team,
    user_principal,
)
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.auth import AuthContext
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import ReserveCommand
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import InsufficientBudget
from tollgate.domain.ids import (
    CredentialId,
    OrgId,
    PrincipalId,
    ProjectId,
    TeamId,
    UserId,
)
from tollgate.domain.scopes import ScopeKind

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


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


async def _seed_tree(engine: AsyncEngine, *, user_limit: int, org_limit: int = 1000) -> None:
    async with engine.begin() as conn:
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
        await conn.execute(
            user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
        )
        await conn.execute(
            budget.insert().values(
                budget_id="b-org",
                scope_kind="org",
                scope_id="o1",
                period_kind="calendar_month",
                hard_limit_micro=org_limit,
            )
        )
        await conn.execute(
            budget.insert().values(
                budget_id="b-user",
                scope_kind="user",
                scope_id="u1",
                period_kind="calendar_month",
                hard_limit_micro=user_limit,
            )
        )


def _auth(scope_kind: ScopeKind = ScopeKind.USER, scope_id: str = "u1") -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("u1"),
        scope_kind=scope_kind,
        scope_id=scope_id,
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))
    return AuthContext(credential=credential, principal=principal)


def _handler(engine: AsyncEngine) -> ReserveHandler:
    return ReserveHandler(
        uow=PostgresUnitOfWork(engine),
        clock=_FixedClock(),
        ids=Uuid7IdGenerator(),
        reservation_ttl_seconds=600,
    )


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


async def _scalar(engine: AsyncEngine, sql: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql))).scalar_one())


async def test_reserve_persists_the_whole_envelope(committing_engine: AsyncEngine) -> None:
    await _seed_prices(committing_engine)
    await _seed_tree(committing_engine, user_limit=1000)
    result = await _handler(committing_engine).reserve(_auth(), _command())

    assert result.estimated_micro == 300
    assert result.price_book_version == "pb-1"
    assert result.ttl_deadline == _NOW + timedelta(seconds=600)
    # reserved on both applicable nodes
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-org'"
        )
        == 300
    )
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-user'"
        )
        == 300
    )
    # reservation + lines + ledger + idempotency persisted
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM reservation WHERE status='held'")
        == 1
    )
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation_line") == 2
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='reserve'") == 2
    assert (
        await _scalar(
            committing_engine, "SELECT count(*) FROM idempotency_key WHERE status='succeeded'"
        )
        == 1
    )


async def test_duplicate_idempotency_key_replays_without_double_reserving(
    committing_engine: AsyncEngine,
) -> None:
    await _seed_prices(committing_engine)
    await _seed_tree(committing_engine, user_limit=1000)
    handler = _handler(committing_engine)
    first = await handler.reserve(_auth(), _command())
    second = await handler.reserve(_auth(), _command())
    assert second == first  # same stored response replayed
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-user'"
        )
        == 300
    )
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation") == 1


async def test_insufficient_budget_denies_and_rolls_everything_back(
    committing_engine: AsyncEngine,
) -> None:
    await _seed_prices(committing_engine)
    await _seed_tree(committing_engine, user_limit=100)  # estimate 300 > 100 -> user binds
    with pytest.raises(InsufficientBudget) as excinfo:
        await _handler(committing_engine).reserve(_auth(), _command())
    assert excinfo.value.scope == "user:u1"
    # nothing persisted: no reservation, no idempotency key, no leftover org reserve
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation") == 0
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0
    assert (
        await _scalar(
            committing_engine, "SELECT coalesce(sum(reserved_micro),0) FROM budget_balance"
        )
        == 0
    )


async def test_reserve_includes_an_authorized_project_budget(
    committing_engine: AsyncEngine,
) -> None:
    await _seed_prices(committing_engine)
    await _seed_tree(committing_engine, user_limit=1000)
    async with committing_engine.begin() as conn:
        await conn.execute(
            project.insert().values(project_id="proj-1", org_id="o1", key="checkout")
        )
        await conn.execute(
            budget.insert().values(
                budget_id="b-proj",
                scope_kind="project",
                scope_id="proj-1",
                period_kind="calendar_month",
                hard_limit_micro=1000,
            )
        )
    # an org-scoped credential authorizes a project under its org
    await _handler(committing_engine).reserve(
        _auth(ScopeKind.ORG, "o1"), _command(project_id=ProjectId("proj-1"))
    )
    assert (
        await _scalar(
            committing_engine, "SELECT reserved_micro FROM budget_balance WHERE budget_id='b-proj'"
        )
        == 300
    )
