"""Integration tests for PostgresReservationRepository (real Postgres, §5.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.reservations_repo import PostgresReservationRepository
from tollgate.domain.ids import BudgetId, PrincipalId, ReservationId
from tollgate.domain.records import ReservationLineRecord, ReservationRecord
from tollgate.domain.reservations import ReservationStatus

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


async def _seed_context(conn: AsyncConnection) -> None:
    await conn.execute(text("INSERT INTO org (org_id, name) VALUES ('o1', 'Org')"))
    await conn.execute(text("INSERT INTO team (team_id, org_id, name) VALUES ('t1', 'o1', 'T')"))
    await conn.execute(text("INSERT INTO user_principal (user_id, team_id) VALUES ('u1', 't1')"))
    await conn.execute(text("INSERT INTO price_book (version) VALUES ('v1')"))
    await conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b1', 'user', 'u1', 'calendar_month', 1000)"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO budget_balance (budget_id, period_start, limit_micro) "
            "VALUES ('b1', TIMESTAMPTZ '2026-06-01 00:00:00+00', 1000)"
        )
    )


def _record(reservation_id: str = "r1", idem: str = "idem-1") -> ReservationRecord:
    return ReservationRecord(
        reservation_id=ReservationId(reservation_id),
        idempotency_key=idem,
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="v1",
        estimated_micro=100,
        input_bound_tokens=50,
        max_output_tokens=50,
        ttl_deadline=PERIOD,
        labels={"team": "blue"},
    )


def _line(reservation_id: str = "r1", budget_id: str = "b1") -> ReservationLineRecord:
    return ReservationLineRecord(
        reservation_id=ReservationId(reservation_id),
        budget_id=BudgetId(budget_id),
        period_start=PERIOD,
        amount_micro=100,
    )


async def _fetch_reservation(conn: AsyncConnection, reservation_id: str) -> Row[tuple[object, ...]]:
    return (
        await conn.execute(
            text(
                "SELECT status, estimated_micro, labels "
                "FROM reservation WHERE reservation_id = :rid"
            ),
            {"rid": reservation_id},
        )
    ).one()


async def _line_budget_ids(conn: AsyncConnection, reservation_id: str) -> list[str]:
    return list(
        (
            await conn.execute(
                text(
                    "SELECT budget_id FROM reservation_line "
                    "WHERE reservation_id = :rid ORDER BY budget_id"
                ),
                {"rid": reservation_id},
            )
        ).scalars()
    )


async def test_insert_persists_held_reservation_and_its_line(db_conn: AsyncConnection) -> None:
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(), [_line()])
    row = await _fetch_reservation(db_conn, "r1")
    assert row.status == "held"
    assert row.estimated_micro == 100
    assert row.labels == {"team": "blue"}
    line_count = (
        await db_conn.execute(
            text("SELECT count(*) AS n FROM reservation_line WHERE reservation_id = 'r1'")
        )
    ).scalar_one()
    assert line_count == 1


async def test_insert_writes_every_line(db_conn: AsyncConnection) -> None:
    await _seed_context(db_conn)
    # A second governed node (the team budget) so the reservation has two lines.
    await db_conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b2', 'team', 't1', 'calendar_month', 5000)"
        )
    )
    await db_conn.execute(
        text(
            "INSERT INTO budget_balance (budget_id, period_start, limit_micro) "
            "VALUES ('b2', TIMESTAMPTZ '2026-06-01 00:00:00+00', 5000)"
        )
    )
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(), [_line(budget_id="b1"), _line(budget_id="b2")])
    budgets = await _line_budget_ids(db_conn, "r1")
    assert budgets == ["b1", "b2"]


async def test_claim_terminal_held_to_committed_wins(db_conn: AsyncConnection) -> None:
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(), [_line()])
    assert await repo.claim_terminal(ReservationId("r1"), ReservationStatus.COMMITTED) is True
    status = (
        await db_conn.execute(text("SELECT status FROM reservation WHERE reservation_id = 'r1'"))
    ).scalar_one()
    assert status == "committed"


async def test_second_claim_terminal_loses_and_leaves_status(db_conn: AsyncConnection) -> None:
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(), [_line()])
    assert await repo.claim_terminal(ReservationId("r1"), ReservationStatus.COMMITTED) is True
    # A second terminal claim (e.g. a late reaper) finds status != 'held' -> 0 rows -> False.
    assert await repo.claim_terminal(ReservationId("r1"), ReservationStatus.RELEASED) is False
    status = (
        await db_conn.execute(text("SELECT status FROM reservation WHERE reservation_id = 'r1'"))
    ).scalar_one()
    assert status == "committed"


async def test_claim_terminal_on_unknown_reservation_is_false(db_conn: AsyncConnection) -> None:
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    assert await repo.claim_terminal(ReservationId("nope"), ReservationStatus.COMMITTED) is False
