"""Integration tests for PostgresUnitOfWork: commit/rollback of the command envelope (§5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.ports import CommandContext, UnitOfWork
from tollgate.domain.ids import BudgetId
from tollgate.domain.scopes import BudgetNode, ScopeKind

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_budget(engine: AsyncEngine, *, limit: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO budget"
                " (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro)"
                " VALUES ('b-org', 'org', 'o1', 'calendar_month', :lim)"
            ),
            {"lim": limit},
        )


async def _reserved(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(reserved_micro), 0) FROM budget_balance"
                    " WHERE budget_id = 'b-org'"
                )
            )
        ).scalar_one()
    return int(value)


async def test_unit_of_work_commits_on_clean_exit(committing_engine: AsyncEngine) -> None:
    await _seed_budget(committing_engine, limit=1000)
    uow: UnitOfWork = PostgresUnitOfWork(committing_engine)
    node = BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")
    async with uow.begin() as tx:
        context: CommandContext = tx
        outcome = await context.reserve_tx.reserve([node], _PERIOD, 200)
        assert outcome.ok is True
    assert await _reserved(committing_engine) == 200  # visible after commit


async def test_unit_of_work_rolls_back_on_exception(committing_engine: AsyncEngine) -> None:
    await _seed_budget(committing_engine, limit=1000)
    uow = PostgresUnitOfWork(committing_engine)
    node = BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")
    with pytest.raises(RuntimeError, match="boom"):
        async with uow.begin() as tx:
            await tx.reserve_tx.reserve([node], _PERIOD, 200)
            raise RuntimeError("boom")
    assert await _reserved(committing_engine) == 0  # the reserve was rolled back
