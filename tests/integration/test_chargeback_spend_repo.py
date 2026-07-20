"""Integration tests for PostgresChargebackRepository.spend_rollup (real Postgres, section 2)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.chargeback_repo import PostgresChargebackRepository
from tollgate.adapters.postgres.schema import (
    budget,
    ledger,
    org,
    price_book,
    reservation,
    team,
    user_principal,
)
from tollgate.domain.chargeback import GroupBy, GroupByKind
from tollgate.domain.scopes import ScopeKind

_PERIOD = datetime(2026, 7, 1, tzinfo=UTC)


async def _tree(conn: AsyncConnection) -> None:
    await conn.execute(org.insert().values(org_id="o1", name="Acme"))
    await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
    await conn.execute(
        user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
    )
    await conn.execute(price_book.insert().values(version="pb-1", published_at=_PERIOD))
    for bid, kind, sid in (("b-org", "org", "o1"), ("b-user", "user", "u1")):
        await conn.execute(
            budget.insert().values(
                budget_id=bid,
                scope_kind=kind,
                scope_id=sid,
                period_kind="calendar_month",
                hard_limit_micro=100_000,
            )
        )


async def _reservation(
    conn: AsyncConnection, *, rid: str, provider: str, model: str, labels: dict[str, str]
) -> None:
    await conn.execute(
        reservation.insert().values(
            reservation_id=rid,
            idempotency_key=f"idem-{rid}",
            status="committed",
            principal_id="u1",
            provider=provider,
            model=model,
            price_book_version="pb-1",
            estimated_micro=0,
            input_bound_tokens=0,
            max_output_tokens=0,
            labels=labels,
            ttl_deadline=_PERIOD,
        )
    )


async def _commit_row(
    conn: AsyncConnection,
    *,
    entry_id: str,
    budget_id: str,
    reservation_id: str | None,
    provider: str,
    committed: int,
    overage: int = 0,
    kind: str = "commit_adjust",
) -> None:
    await conn.execute(
        ledger.insert().values(
            entry_id=entry_id,
            kind=kind,
            budget_id=budget_id,
            period_start=_PERIOD,
            reservation_id=reservation_id,
            delta_committed_micro=committed,
            delta_overage_micro=overage,
            provider=provider,
        )
    )


async def _meter_row(
    conn: AsyncConnection,
    *,
    entry_id: str,
    budget_id: str,
    provider: str,
    model: str,
    labels: dict[str, str],
    committed: int,
    overage: int = 0,
) -> None:
    await conn.execute(
        ledger.insert().values(
            entry_id=entry_id,
            kind="meter",
            budget_id=budget_id,
            period_start=_PERIOD,
            reservation_id=None,
            delta_committed_micro=committed,
            delta_overage_micro=overage,
            provider=provider,
            model=model,
            labels=labels,
        )
    )


async def test_meter_rows_group_by_ledger_model(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    await _meter_row(
        db_conn,
        entry_id="m1",
        budget_id="b-user",
        provider="anthropic",
        model="claude",
        labels={"env": "prod"},
        committed=100,
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.MODEL))
    # the meter row's spend joins the reservation-backed "claude" spend, NOT the None bucket
    assert {(g.group, g.spend_micro) for g in rows} == {("claude", 300)}


async def test_meter_rows_group_by_ledger_label(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _meter_row(
        db_conn,
        entry_id="m1",
        budget_id="b-user",
        provider="anthropic",
        model="claude",
        labels={"env": "prod"},
        committed=100,
    )
    await _meter_row(
        db_conn,
        entry_id="m2",
        budget_id="b-user",
        provider="anthropic",
        model="claude",
        labels={"env": "dev"},
        committed=40,
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(
        ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.LABEL, label_key="env")
    )
    assert {(g.group, g.spend_micro) for g in rows} == {("prod", 100), ("dev", 40)}


async def test_single_node_aggregation_does_not_double_count(db_conn: AsyncConnection) -> None:
    # one reservation drew on BOTH the user and org budgets, so commit wrote a row on each.
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _commit_row(
        db_conn,
        entry_id="e-org",
        budget_id="b-org",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    await _commit_row(
        db_conn,
        entry_id="e-user",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    repo = PostgresChargebackRepository(db_conn)
    org = await repo.spend_rollup(ScopeKind.ORG, "o1", _PERIOD, GroupBy(GroupByKind.PROVIDER))
    user = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.PROVIDER))
    assert [(g.group, g.spend_micro) for g in org] == [("anthropic", 200)]  # once, not 400
    assert [(g.group, g.spend_micro) for g in user] == [("anthropic", 200)]


async def test_spend_is_committed_plus_overage(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=300,
    )
    await _commit_row(
        db_conn,
        entry_id="e2",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=0,
        overage=50,
        kind="overage",
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.PROVIDER))
    assert [(g.group, g.spend_micro) for g in rows] == [("anthropic", 350)]


async def test_group_by_model_and_zero_spend_rows_dropped(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _reservation(db_conn, rid="r2", provider="openai", model="gpt", labels={})
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    await _commit_row(
        db_conn,
        entry_id="e2",
        budget_id="b-user",
        reservation_id="r2",
        provider="openai",
        committed=100,
    )
    # a reserve row (zero committed/overage) must not create a spurious group
    await db_conn.execute(
        ledger.insert().values(
            entry_id="e3",
            kind="reserve",
            budget_id="b-user",
            period_start=_PERIOD,
            reservation_id="r1",
            delta_reserved_micro=999,
            provider="anthropic",
        )
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.MODEL))
    assert {(g.group, g.spend_micro) for g in rows} == {("claude", 200), ("gpt", 100)}


async def test_grace_row_lands_in_the_none_bucket_for_model(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    # a grace_backfill row: NULL reservation_id, both deltas
    await _commit_row(
        db_conn,
        entry_id="e2",
        budget_id="b-user",
        reservation_id=None,
        provider="anthropic",
        committed=70,
        overage=30,
        kind="grace_backfill",
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.MODEL))
    assert {(g.group, g.spend_micro) for g in rows} == {("claude", 200), (None, 100)}


async def test_group_by_label_key(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(
        db_conn, rid="r1", provider="anthropic", model="claude", labels={"env": "prod"}
    )
    await _reservation(
        db_conn, rid="r2", provider="anthropic", model="claude", labels={"env": "dev"}
    )
    await _reservation(db_conn, rid="r3", provider="anthropic", model="claude", labels={})  # no env
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    await _commit_row(
        db_conn,
        entry_id="e2",
        budget_id="b-user",
        reservation_id="r2",
        provider="anthropic",
        committed=50,
    )
    await _commit_row(
        db_conn,
        entry_id="e3",
        budget_id="b-user",
        reservation_id="r3",
        provider="anthropic",
        committed=10,
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(
        ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.LABEL, label_key="env")
    )
    assert {(g.group, g.spend_micro) for g in rows} == {("prod", 200), ("dev", 50), (None, 10)}


async def test_other_period_and_other_node_excluded(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)
    await _reservation(db_conn, rid="r1", provider="anthropic", model="claude", labels={})
    await _commit_row(
        db_conn,
        entry_id="e1",
        budget_id="b-user",
        reservation_id="r1",
        provider="anthropic",
        committed=200,
    )
    # a row in a different period must be excluded
    await db_conn.execute(
        ledger.insert().values(
            entry_id="e2",
            kind="commit_adjust",
            budget_id="b-user",
            period_start=datetime(2026, 6, 1, tzinfo=UTC),
            reservation_id="r1",
            delta_committed_micro=999,
            provider="anthropic",
        )
    )
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.PROVIDER))
    assert [(g.group, g.spend_micro) for g in rows] == [("anthropic", 200)]


async def test_node_without_a_budget_is_empty(db_conn: AsyncConnection) -> None:
    await _tree(db_conn)  # team t1 has no budget
    repo = PostgresChargebackRepository(db_conn)
    rows = await repo.spend_rollup(ScopeKind.TEAM, "t1", _PERIOD, GroupBy(GroupByKind.PROVIDER))
    assert rows == []
