"""Integration tests for PostgresChargebackRepository (real Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.chargeback_repo import PostgresChargebackRepository
from tollgate.adapters.postgres.schema import (
    budget,
    budget_alert,
    budget_balance,
    org,
    project,
    team,
    user_principal,
)
from tollgate.domain.ids import BudgetId
from tollgate.domain.scopes import ScopeKind

_PERIOD = datetime(2026, 7, 1, tzinfo=UTC)


async def _seed_tree(conn: AsyncConnection) -> None:
    await conn.execute(org.insert().values(org_id="o1", name="Acme"))
    await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
    await conn.execute(team.insert().values(team_id="t2", org_id="o1", name="Growth"))
    await conn.execute(
        user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
    )
    await conn.execute(
        user_principal.insert().values(user_id="u2", team_id="t2", external_ref=None)
    )
    await conn.execute(project.insert().values(project_id="p1", org_id="o1", key="checkout"))


async def _budget(
    conn: AsyncConnection, *, budget_id: str, scope_kind: str, scope_id: str, limit: int
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


async def _balance(
    conn: AsyncConnection,
    *,
    budget_id: str,
    limit: int,
    reserved: int = 0,
    committed: int = 0,
    overage: int = 0,
) -> None:
    await conn.execute(
        budget_balance.insert().values(
            budget_id=budget_id,
            period_start=_PERIOD,
            limit_micro=limit,
            reserved_micro=reserved,
            committed_micro=committed,
            overage_micro=overage,
        )
    )


async def test_org_subtree_returns_all_descendant_budgets_sorted(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=10_000)
    await _budget(db_conn, budget_id="b-t1", scope_kind="team", scope_id="t1", limit=5_000)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    await _budget(db_conn, budget_id="b-p1", scope_kind="project", scope_id="p1", limit=2_000)
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.ORG, "o1", _PERIOD
    )
    # sorted by (scope_rank, scope_id): org, team, user, project
    assert [(s.scope_kind, s.scope_id) for s in states] == [
        (ScopeKind.ORG, "o1"),
        (ScopeKind.TEAM, "t1"),
        (ScopeKind.USER, "u1"),
        (ScopeKind.PROJECT, "p1"),
    ]


async def test_no_balance_row_yields_zero_state_against_hard_limit(
    db_conn: AsyncConnection,
) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.USER, "u1", _PERIOD
    )
    assert len(states) == 1
    balance = states[0].balance
    assert balance.limit_micro == 1_000
    assert (balance.reserved_micro, balance.committed_micro, balance.overage_micro) == (0, 0, 0)


async def test_balance_row_supplies_live_amounts_and_its_own_limit(
    db_conn: AsyncConnection,
) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    # an in-period limit change: the balance limit differs from the budget hard limit
    await _balance(db_conn, budget_id="b-u1", limit=1_500, reserved=200, committed=300, overage=50)
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.USER, "u1", _PERIOD
    )
    balance = states[0].balance
    assert balance.limit_micro == 1_500  # from budget_balance, not hard_limit_micro
    assert (balance.reserved_micro, balance.committed_micro, balance.overage_micro) == (
        200,
        300,
        50,
    )


async def test_alerts_are_attached_ascending(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    await db_conn.execute(budget_alert.insert().values(budget_id="b-u1", threshold_pct=90))
    await db_conn.execute(budget_alert.insert().values(budget_id="b-u1", threshold_pct=50))
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.USER, "u1", _PERIOD
    )
    assert states[0].alert_thresholds_pct == (50, 90)


async def test_team_subtree_excludes_org_and_project_and_other_teams(
    db_conn: AsyncConnection,
) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=10_000)
    await _budget(db_conn, budget_id="b-t1", scope_kind="team", scope_id="t1", limit=5_000)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    await _budget(db_conn, budget_id="b-u2", scope_kind="user", scope_id="u2", limit=1_000)
    await _budget(db_conn, budget_id="b-p1", scope_kind="project", scope_id="p1", limit=2_000)
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.TEAM, "t1", _PERIOD
    )
    assert {(s.scope_kind, s.scope_id) for s in states} == {
        (ScopeKind.TEAM, "t1"),
        (ScopeKind.USER, "u1"),
    }


async def test_user_and_project_subtrees_are_just_their_own_node(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    await _budget(db_conn, budget_id="b-p1", scope_kind="project", scope_id="p1", limit=2_000)
    repo = PostgresChargebackRepository(db_conn)
    user_states = await repo.subtree_states(ScopeKind.USER, "u1", _PERIOD)
    project_states = await repo.subtree_states(ScopeKind.PROJECT, "p1", _PERIOD)
    assert [(s.scope_kind, s.scope_id) for s in user_states] == [(ScopeKind.USER, "u1")]
    assert [(s.scope_kind, s.scope_id) for s in project_states] == [(ScopeKind.PROJECT, "p1")]


async def test_scope_with_no_budgets_is_empty(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)  # no budgets seeded at all
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.ORG, "o1", _PERIOD
    )
    assert states == []


async def test_resolve_scope_ancestry_for_each_kind(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    repo = PostgresChargebackRepository(db_conn)
    assert await repo.resolve_scope_ancestry(ScopeKind.ORG, "o1") == {ScopeKind.ORG: "o1"}
    assert await repo.resolve_scope_ancestry(ScopeKind.TEAM, "t1") == {
        ScopeKind.ORG: "o1",
        ScopeKind.TEAM: "t1",
    }
    assert await repo.resolve_scope_ancestry(ScopeKind.USER, "u1") == {
        ScopeKind.ORG: "o1",
        ScopeKind.TEAM: "t1",
        ScopeKind.USER: "u1",
    }
    assert await repo.resolve_scope_ancestry(ScopeKind.PROJECT, "p1") == {
        ScopeKind.ORG: "o1",
        ScopeKind.PROJECT: "p1",
    }


async def test_resolve_scope_ancestry_is_none_for_unknown_nodes(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    repo = PostgresChargebackRepository(db_conn)
    assert await repo.resolve_scope_ancestry(ScopeKind.TEAM, "ghost") is None
    assert await repo.resolve_scope_ancestry(ScopeKind.USER, "ghost") is None
    assert await repo.resolve_scope_ancestry(ScopeKind.PROJECT, "ghost") is None


async def test_zero_state_ids_and_kinds_are_typed(db_conn: AsyncConnection) -> None:
    await _seed_tree(db_conn)
    await _budget(db_conn, budget_id="b-u1", scope_kind="user", scope_id="u1", limit=1_000)
    states = await PostgresChargebackRepository(db_conn).subtree_states(
        ScopeKind.USER, "u1", _PERIOD
    )
    assert states[0].budget_id == BudgetId("b-u1")
    assert states[0].scope_kind is ScopeKind.USER
