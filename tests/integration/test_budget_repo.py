"""Integration tests for PostgresBudgetRepository (real Postgres)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.budget_repo import PostgresBudgetRepository
from tollgate.adapters.postgres.schema import budget, org, project, team, user_principal
from tollgate.domain.credentials import Principal
from tollgate.domain.ids import OrgId, ProjectId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind


async def _seed_tree(conn: AsyncConnection) -> None:
    await conn.execute(org.insert().values(org_id="o1", name="Acme"))
    await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
    await conn.execute(
        user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
    )


async def _budget(
    conn: AsyncConnection, *, budget_id: str, scope_kind: str, scope_id: str, limit: int = 1000
) -> None:
    await conn.execute(
        budget.insert().values(
            budget_id=budget_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            period_kind="calendar_month",
            hard_limit_micro=limit,
        )
    )


def _principal() -> Principal:
    return Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))


async def test_find_ancestry_budgets_returns_existing_and_skips_missing(
    db_conn: AsyncConnection,
) -> None:
    await _seed_tree(db_conn)
    # org and user carry a budget; the team does not
    await _budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1")
    await _budget(db_conn, budget_id="b-user", scope_kind="user", scope_id="u1")
    nodes = await PostgresBudgetRepository(db_conn).find_ancestry_budgets(_principal())
    by_kind = {node.scope_kind: node for node in nodes}
    assert set(by_kind) == {ScopeKind.ORG, ScopeKind.USER}
    assert by_kind[ScopeKind.ORG].budget_id == "b-org"
    assert by_kind[ScopeKind.USER].budget_id == "b-user"
    assert by_kind[ScopeKind.USER].scope_id == "u1"


async def test_find_project_returns_org_and_budget_when_present(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await db_conn.execute(project.insert().values(project_id="proj-1", org_id="o1", key="checkout"))
    await _budget(db_conn, budget_id="b-proj", scope_kind="project", scope_id="proj-1")
    resolved = await PostgresBudgetRepository(db_conn).find_project(ProjectId("proj-1"))
    assert resolved is not None
    assert resolved.org_id == "o1"
    assert resolved.budget is not None
    assert resolved.budget.budget_id == "b-proj"
    assert resolved.budget.scope_kind is ScopeKind.PROJECT
    assert resolved.budget.scope_id == "proj-1"


async def test_find_project_returns_org_with_no_budget(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await db_conn.execute(project.insert().values(project_id="proj-1", org_id="o1", key="checkout"))
    resolved = await PostgresBudgetRepository(db_conn).find_project(ProjectId("proj-1"))
    assert resolved is not None
    assert resolved.org_id == "o1"
    assert resolved.budget is None


async def test_find_project_returns_none_for_unknown_project(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    assert await PostgresBudgetRepository(db_conn).find_project(ProjectId("ghost")) is None
