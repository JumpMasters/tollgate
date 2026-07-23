"""Single-connection behaviour of PostgresReserveTransaction (real Postgres).

These pin the ordering, all-or-nothing reporting, denial-naming and lazy period roll on one
connection (rolled back after each test). The emergent concurrency guarantees — exactly the
headroom-many reserves admitted, sibling-parent deadlock-freedom — are proven by the committing
tests in test_reserve_concurrency.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.reserve_tx import PostgresReserveTransaction
from tollgate.domain.ids import BudgetId
from tollgate.domain.scopes import BudgetNode, ScopeKind

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_budget(
    conn: AsyncConnection, *, budget_id: str, scope_kind: str, scope_id: str, limit: int
) -> None:
    await conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES (:b, :k, :s, 'calendar_month', :lim)"
        ),
        {"b": budget_id, "k": scope_kind, "s": scope_id, "lim": limit},
    )


async def _reserved(conn: AsyncConnection, budget_id: str) -> int:
    result = (
        await conn.execute(
            text("SELECT reserved_micro FROM budget_balance WHERE budget_id = :b"),
            {"b": budget_id},
        )
    ).scalar_one()
    return int(result)


async def test_reserve_succeeds_on_all_nodes_when_each_has_headroom(
    db_conn: AsyncConnection,
) -> None:
    await _seed_budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1000)
    await _seed_budget(db_conn, budget_id="b-user", scope_kind="user", scope_id="u1", limit=500)
    nodes = [
        BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1"),
        BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
    ]
    outcome = await PostgresReserveTransaction(db_conn).reserve(nodes, PERIOD, 200)
    assert outcome.ok is True
    assert outcome.binding_node is None
    assert await _reserved(db_conn, "b-org") == 200
    assert await _reserved(db_conn, "b-user") == 200


async def test_reserve_lazily_creates_each_period_balance(db_conn: AsyncConnection) -> None:
    # No budget_balance is pre-seeded — ensure_period rolls the node's period on first reserve.
    await _seed_budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1000)
    nodes = [BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1")]
    outcome = await PostgresReserveTransaction(db_conn).reserve(nodes, PERIOD, 200)
    assert outcome.ok is True
    count = (
        await db_conn.execute(
            text("SELECT count(*) AS n FROM budget_balance WHERE budget_id = 'b-org'")
        )
    ).scalar_one()
    assert count == 1


async def test_reserve_denies_and_names_the_binding_node(db_conn: AsyncConnection) -> None:
    # The org parent has ample room; the user budget is too small → user is the binding node.
    await _seed_budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1000)
    await _seed_budget(db_conn, budget_id="b-user", scope_kind="user", scope_id="u1", limit=100)
    nodes = [
        BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1"),
        BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
    ]
    outcome = await PostgresReserveTransaction(db_conn).reserve(nodes, PERIOD, 200)
    assert outcome.ok is False
    assert outcome.binding_node is not None
    assert outcome.binding_node.scope_kind is ScopeKind.USER
    assert outcome.binding_node.scope_id == "u1"
    # The earlier node WAS reserved within this transaction; the all-or-nothing rollback that
    # discards it is the caller's (the command envelope), proven under real concurrency in
    # test_reserve_concurrency.py.
    assert await _reserved(db_conn, "b-org") == 200


async def test_reserve_accepts_nodes_in_any_input_order(db_conn: AsyncConnection) -> None:
    # Pass the set deepest-first; reserve_tx sorts into lock order (org < user) internally.
    await _seed_budget(db_conn, budget_id="b-org", scope_kind="org", scope_id="o1", limit=1000)
    await _seed_budget(db_conn, budget_id="b-user", scope_kind="user", scope_id="u1", limit=500)
    nodes = [
        BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
        BudgetNode(BudgetId("b-org"), ScopeKind.ORG, "o1"),
    ]
    outcome = await PostgresReserveTransaction(db_conn).reserve(nodes, PERIOD, 200)
    assert outcome.ok is True
    assert await _reserved(db_conn, "b-org") == 200
    assert await _reserved(db_conn, "b-user") == 200
