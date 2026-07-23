"""Integration tests for PostgresReservationRepository (real Postgres)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.reservations_repo import PostgresReservationRepository
from tollgate.adapters.postgres.schema import org, price_book, reservation, team, user_principal
from tollgate.domain.errors import IdempotencyKeyReuse
from tollgate.domain.ids import BudgetId, PrincipalId, ReservationId
from tollgate.domain.records import ReservationLineRecord, ReservationRecord
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import ScopeKind

PERIOD = datetime(2026, 6, 1, tzinfo=UTC)
_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


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


async def test_find_lines_returns_the_canonical_lock_order(db_conn: AsyncConnection) -> None:
    # find_lines returns lines already in the canonical lock order (org < user), so deadlock
    # freedom is self-enforcing at the source rather than resting on every caller re-sorting (#107).
    await _seed_context(db_conn)  # seeds the user budget b1
    await db_conn.execute(
        text(
            "INSERT INTO budget (budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b-org', 'org', 'o1', 'calendar_month', 1000)"
        )
    )
    await db_conn.execute(
        text(
            "INSERT INTO budget_balance (budget_id, period_start, limit_micro) "
            "VALUES ('b-org', TIMESTAMPTZ '2026-06-01 00:00:00+00', 1000)"
        )
    )
    repo = PostgresReservationRepository(db_conn)
    # Insert the user line before the org line (non-canonical) to prove the repo re-sorts.
    await repo.insert(_record(), [_line(budget_id="b1"), _line(budget_id="b-org")])
    lines = await repo.find_lines(ReservationId("r1"))
    assert [line.node.scope_kind for line in lines] == [ScopeKind.ORG, ScopeKind.USER]


async def test_reusing_a_key_maps_the_unique_violation_to_key_reuse(
    db_conn: AsyncConnection,
) -> None:
    # Reusing an idempotency key (e.g. after the key reaper deleted its row) collides with the
    # surviving reservation's per-principal unique guard; the repo maps that IntegrityError to
    # IdempotencyKeyReuse (409) rather than letting it surface as an unmapped 500 (#61).
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(reservation_id="r1", idem="dup"), [_line("r1")])
    with pytest.raises(IdempotencyKeyReuse):
        await repo.insert(_record(reservation_id="r2", idem="dup"), [_line("r2")])


async def test_same_key_under_different_principals_is_allowed(db_conn: AsyncConnection) -> None:
    # The reservation guard is per principal, so two principals reusing the same key string do not
    # collide — matching the per-principal idempotency-key namespace (#71).
    await _seed_context(db_conn)
    await db_conn.execute(text("INSERT INTO user_principal (user_id, team_id) VALUES ('u2', 't1')"))
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(reservation_id="r1", idem="dup"), [_line("r1")])
    other = replace(_record(reservation_id="r2", idem="dup"), principal_id=PrincipalId("u2"))
    await repo.insert(other, [_line("r2")])
    count = (
        await db_conn.execute(
            text("SELECT count(*) AS n FROM reservation WHERE idempotency_key = 'dup'")
        )
    ).scalar_one()
    assert count == 2


async def test_claim_next_expired_skips_excluded_ids(db_conn: AsyncConnection) -> None:
    # exclude_ids removes candidates so a reservation whose reap failed this tick cannot
    # monopolize the queue head and starve the ones behind it (#74).
    await _seed_context(db_conn)
    repo = PostgresReservationRepository(db_conn)
    await repo.insert(_record(reservation_id="r1", idem="k1"), [_line("r1")])
    await repo.insert(_record(reservation_id="r2", idem="k2"), [_line("r2")])
    # both are held and past their TTL (ttl_deadline = PERIOD, well before _NOW)
    excluding_r1 = await repo.claim_next_expired(_NOW, [ReservationId("r1")])
    assert excluding_r1 is not None
    assert excluding_r1.record.reservation_id == "r2"  # r1 skipped despite being a candidate
    # r2 is now reaped; excluding both leaves nothing claimable
    assert await repo.claim_next_expired(_NOW, [ReservationId("r1"), ReservationId("r2")]) is None


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


async def _seed_identity(conn: AsyncConnection) -> None:
    await conn.execute(org.insert().values(org_id="o1", name="Acme"))
    await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
    await conn.execute(
        user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
    )
    await conn.execute(price_book.insert().values(version="pb-1"))


async def _insert_reservation(
    conn: AsyncConnection, *, rid: str, ttl: datetime, status: str = "held"
) -> None:
    await conn.execute(
        reservation.insert().values(
            reservation_id=rid,
            idempotency_key=f"idem-{rid}",
            principal_id="u1",
            provider="anthropic",
            model="claude",
            price_book_version="pb-1",
            estimated_micro=300,
            input_bound_tokens=100,
            max_output_tokens=100,
            ttl_deadline=ttl,
            labels={},
            status=status,
        )
    )


async def test_claim_next_expired_reaps_oldest_first_then_returns_none(
    db_conn: AsyncConnection,
) -> None:
    await _seed_identity(db_conn)
    # two expired held (different ttl), one future held, one already committed
    await _insert_reservation(db_conn, rid="r-old", ttl=_NOW - timedelta(minutes=10))
    await _insert_reservation(db_conn, rid="r-new", ttl=_NOW - timedelta(minutes=1))
    await _insert_reservation(db_conn, rid="r-future", ttl=_NOW + timedelta(minutes=10))
    await _insert_reservation(
        db_conn, rid="r-done", ttl=_NOW - timedelta(minutes=5), status="committed"
    )
    repo = PostgresReservationRepository(db_conn)

    first = await repo.claim_next_expired(_NOW)
    assert first is not None
    assert first.record.reservation_id == "r-old"  # oldest ttl_deadline first
    assert first.status == ReservationStatus.REAPED
    assert first.record.provider == "anthropic"  # full record for ledger provenance

    second = await repo.claim_next_expired(_NOW)
    assert second is not None
    assert second.record.reservation_id == "r-new"

    assert await repo.claim_next_expired(_NOW) is None  # future + terminal are left alone

    # the two claimed rows are now 'reaped'; the untouched two keep their status
    statuses = {
        row.reservation_id: row.status
        for row in (await db_conn.execute(reservation.select())).all()
    }
    assert statuses == {
        "r-old": "reaped",
        "r-new": "reaped",
        "r-future": "held",
        "r-done": "committed",
    }
