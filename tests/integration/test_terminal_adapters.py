"""Integration tests for the terminal-command adapter surface (real Postgres).

Covers the recovery-path counter primitive (``apply_spend``), the reservation read-back and
guards (``find`` / ``find_lines`` / ``claim_late_commit`` / ``advance_ttl``), and the
stamped-version price lookup (``price_at``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.counter_store import PostgresCounterStore
from tollgate.adapters.postgres.price_book_repo import PostgresPriceBookRepository
from tollgate.adapters.postgres.reservations_repo import PostgresReservationRepository
from tollgate.adapters.postgres.schema import (
    budget,
    budget_balance,
    org,
    price,
    price_book,
    team,
    user_principal,
)
from tollgate.domain.errors import BudgetNotFound
from tollgate.domain.ids import BudgetId, PrincipalId, ReservationId
from tollgate.domain.pricing import Reconciliation
from tollgate.domain.records import ReservationLineRecord, ReservationRecord
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import ScopeKind

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
_TTL = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)


async def _seed_balance(
    conn: AsyncConnection,
    *,
    budget_id: str,
    scope_kind: str = "org",
    scope_id: str | None = None,
    limit: int,
    reserved: int = 0,
    committed: int = 0,
    overage: int = 0,
) -> None:
    await conn.execute(
        budget.insert().values(
            budget_id=budget_id,
            scope_kind=scope_kind,
            scope_id=scope_id if scope_id is not None else budget_id,
            period_kind="calendar_month",
            hard_limit_micro=limit,
        )
    )
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


async def _balance(conn: AsyncConnection, budget_id: str) -> tuple[int, int, int]:
    row = (
        await conn.execute(
            select(
                budget_balance.c.reserved_micro,
                budget_balance.c.committed_micro,
                budget_balance.c.overage_micro,
            ).where(
                budget_balance.c.budget_id == budget_id,
                budget_balance.c.period_start == _PERIOD,
            )
        )
    ).one()
    return (row.reserved_micro, row.committed_micro, row.overage_micro)


async def test_apply_spend_commits_fully_within_remaining(db_conn: AsyncConnection) -> None:
    await _seed_balance(db_conn, budget_id="b-fit", limit=1_000)
    applied = await PostgresCounterStore(db_conn).apply_spend(BudgetId("b-fit"), _PERIOD, 300)
    assert applied == Reconciliation(committed_micro=300, overage_micro=0)
    assert await _balance(db_conn, "b-fit") == (0, 300, 0)


async def test_apply_spend_splits_the_excess_into_overage(db_conn: AsyncConnection) -> None:
    await _seed_balance(
        db_conn, budget_id="b-tight", limit=1_000, reserved=600, committed=200, overage=100
    )
    # remaining = 1000 - 600 - 200 - 100 = 100; spend 250 -> 100 committed, 150 overage
    applied = await PostgresCounterStore(db_conn).apply_spend(BudgetId("b-tight"), _PERIOD, 250)
    assert applied == Reconciliation(committed_micro=100, overage_micro=150)
    # the row CHECK (reserved + committed <= limit) holds: 600 + 300 <= 1000
    assert await _balance(db_conn, "b-tight") == (600, 300, 250)


async def test_apply_spend_with_zero_remaining_is_all_overage(db_conn: AsyncConnection) -> None:
    await _seed_balance(db_conn, budget_id="b-full", limit=100, committed=100)
    applied = await PostgresCounterStore(db_conn).apply_spend(BudgetId("b-full"), _PERIOD, 40)
    assert applied == Reconciliation(committed_micro=0, overage_micro=40)
    assert await _balance(db_conn, "b-full") == (0, 100, 40)


async def test_apply_spend_without_a_balance_row_is_refused(db_conn: AsyncConnection) -> None:
    with pytest.raises(BudgetNotFound):
        await PostgresCounterStore(db_conn).apply_spend(BudgetId("ghost"), _PERIOD, 1)


async def _seed_reservation(conn: AsyncConnection) -> PostgresReservationRepository:
    await conn.execute(org.insert().values(org_id="o1", name="Acme"))
    await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
    await conn.execute(
        user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
    )
    await conn.execute(price_book.insert().values(version="pb-1", published_at=_PERIOD))
    await _seed_balance(conn, budget_id="b-user", scope_kind="user", scope_id="u1", limit=1_000)
    repo = PostgresReservationRepository(conn)
    record = ReservationRecord(
        reservation_id=ReservationId("res-1"),
        idempotency_key="idem-res-1",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="pb-1",
        estimated_micro=300,
        input_bound_tokens=100,
        max_output_tokens=100,
        ttl_deadline=_TTL,
        labels={"env": "prod"},
    )
    line = ReservationLineRecord(
        reservation_id=ReservationId("res-1"),
        budget_id=BudgetId("b-user"),
        period_start=_PERIOD,
        amount_micro=300,
    )
    await repo.insert(record, [line])
    return repo


async def test_find_returns_the_record_and_live_status(db_conn: AsyncConnection) -> None:
    repo = await _seed_reservation(db_conn)
    stored = await repo.find(ReservationId("res-1"))
    assert stored is not None
    assert stored.status is ReservationStatus.HELD
    assert stored.record.reservation_id == "res-1"
    assert stored.record.principal_id == "u1"
    assert stored.record.provider == "anthropic"
    assert stored.record.price_book_version == "pb-1"
    assert stored.record.estimated_micro == 300
    assert stored.record.ttl_deadline == _TTL
    assert stored.record.labels == {"env": "prod"}


async def test_find_returns_none_for_an_unknown_id(db_conn: AsyncConnection) -> None:
    assert await PostgresReservationRepository(db_conn).find(ReservationId("ghost")) is None


async def test_find_lines_joins_the_budget_node(db_conn: AsyncConnection) -> None:
    repo = await _seed_reservation(db_conn)
    lines = await repo.find_lines(ReservationId("res-1"))
    assert len(lines) == 1
    (line,) = lines
    assert line.node.budget_id == "b-user"
    assert line.node.scope_kind is ScopeKind.USER
    assert line.node.scope_id == "u1"
    assert line.period_start == _PERIOD
    assert line.amount_micro == 300


async def test_claim_late_commit_wins_only_from_reaped(db_conn: AsyncConnection) -> None:
    repo = await _seed_reservation(db_conn)
    assert await repo.claim_late_commit(ReservationId("res-1")) is False  # held, not reaped
    assert await repo.claim_terminal(ReservationId("res-1"), ReservationStatus.REAPED) is True
    assert await repo.claim_late_commit(ReservationId("res-1")) is True  # reaped -> committed
    assert await repo.claim_late_commit(ReservationId("res-1")) is False  # exactly once
    stored = await repo.find(ReservationId("res-1"))
    assert stored is not None
    assert stored.status is ReservationStatus.COMMITTED


async def test_advance_ttl_is_monotonic_and_held_only(db_conn: AsyncConnection) -> None:
    repo = await _seed_reservation(db_conn)
    later = _TTL + timedelta(minutes=5)
    assert await repo.advance_ttl(ReservationId("res-1"), later) == later
    # a stale heartbeat can never move the deadline backward
    assert await repo.advance_ttl(ReservationId("res-1"), _TTL) == later
    assert await repo.claim_terminal(ReservationId("res-1"), ReservationStatus.RELEASED) is True
    assert await repo.advance_ttl(ReservationId("res-1"), later + timedelta(minutes=5)) is None
    assert await repo.advance_ttl(ReservationId("ghost"), later) is None


async def _publish_prices(conn: AsyncConnection) -> None:
    await conn.execute(
        price_book.insert().values(version="v1", published_at=datetime(2026, 5, 1, tzinfo=UTC))
    )
    await conn.execute(
        price_book.insert().values(version="v2", published_at=datetime(2026, 6, 1, tzinfo=UTC))
    )
    for version, rate in (("v1", "1"), ("v2", "3")):
        await conn.execute(
            price.insert().values(
                price_book_version=version,
                provider="anthropic",
                model="claude",
                input_micro_per_token=Decimal(rate),
                output_micro_per_token=Decimal("2"),
                cached_input_micro_per_token=Decimal("0.5"),
                cache_creation_micro_per_token=Decimal("1.25"),
            )
        )


async def test_price_at_returns_the_stamped_version_not_the_latest(
    db_conn: AsyncConnection,
) -> None:
    await _publish_prices(db_conn)
    got = await PostgresPriceBookRepository(db_conn).price_at("v1", "anthropic", "claude")
    assert got is not None
    assert got.input_micro_per_token == Decimal("1")  # v1's rate, not the newer v2's


async def test_price_at_returns_none_when_the_row_is_absent(db_conn: AsyncConnection) -> None:
    await _publish_prices(db_conn)
    repo = PostgresPriceBookRepository(db_conn)
    assert await repo.price_at("v9", "anthropic", "claude") is None
    assert await repo.price_at("v1", "openai", "gpt") is None


async def test_resolve_price_is_per_pair_latest_not_global_latest(
    db_conn: AsyncConnection,
) -> None:
    # Locks the ADR 0028 consequence: a pair omitted from a newer book still resolves to
    # the latest version that priced *it*.
    await _publish_prices(db_conn)  # v1 (May) and v2 (June) both price anthropic/claude
    await db_conn.execute(
        price.insert().values(
            price_book_version="v1",
            provider="openai",
            model="gpt",
            input_micro_per_token=Decimal("7"),
            output_micro_per_token=Decimal("9"),
            cached_input_micro_per_token=Decimal("3"),
            cache_creation_micro_per_token=Decimal("1.25"),
        )
    )
    priced = await PostgresPriceBookRepository(db_conn).resolve_price("openai", "gpt")
    assert priced is not None
    assert priced.version == "v1"  # per-pair latest, even though v2 is globally newer
    assert priced.price.input_micro_per_token == Decimal("7")
