"""Constraint tests for reservation / reservation_line."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection


async def _seed_reservation_context(db_conn: AsyncConnection) -> None:
    await db_conn.execute(text("INSERT INTO org (org_id, name) VALUES ('o1', 'Org')"))
    await db_conn.execute(text("INSERT INTO team (team_id, org_id, name) VALUES ('t1', 'o1', 'T')"))
    await db_conn.execute(text("INSERT INTO user_principal (user_id, team_id) VALUES ('u1', 't1')"))
    await db_conn.execute(text("INSERT INTO price_book (version) VALUES ('v1')"))
    await db_conn.execute(
        text(
            "INSERT INTO budget "
            "(budget_id, scope_kind, scope_id, period_kind, hard_limit_micro) "
            "VALUES ('b1', 'user', 'u1', 'calendar_month', 1000)"
        )
    )
    await db_conn.execute(
        text(
            "INSERT INTO budget_balance "
            "(budget_id, period_start, limit_micro) "
            "VALUES ('b1', TIMESTAMPTZ '2026-06-01 00:00:00+00', 1000)"
        )
    )


async def _insert_reservation(db_conn: AsyncConnection, *, reservation_id: str, idem: str) -> None:
    await db_conn.execute(
        text(
            "INSERT INTO reservation "
            "(reservation_id, idempotency_key, principal_id, provider, model, "
            " price_book_version, estimated_micro, input_bound_tokens, max_output_tokens, "
            " ttl_deadline) "
            "VALUES (:rid, :idem, 'u1', 'anthropic', 'claude', 'v1', 100, 50, 50, now())"
        ),
        {"rid": reservation_id, "idem": idem},
    )


async def test_reservation_status_check_rejects_unknown(db_conn: AsyncConnection) -> None:
    await _seed_reservation_context(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO reservation "
                "(reservation_id, idempotency_key, status, principal_id, provider, model, "
                " price_book_version, estimated_micro, input_bound_tokens, max_output_tokens, "
                " ttl_deadline) "
                "VALUES ('r1', 'idem-1', 'pending', 'u1', 'anthropic', 'claude', 'v1', "
                " 100, 50, 50, now())"
            )
        )


async def test_reservation_idempotency_key_is_unique(db_conn: AsyncConnection) -> None:
    await _seed_reservation_context(db_conn)
    await _insert_reservation(db_conn, reservation_id="r1", idem="idem-dup")
    with pytest.raises(IntegrityError):
        await _insert_reservation(db_conn, reservation_id="r2", idem="idem-dup")


async def test_reservation_line_requires_an_existing_balance_row(db_conn: AsyncConnection) -> None:
    await _seed_reservation_context(db_conn)
    await _insert_reservation(db_conn, reservation_id="r1", idem="idem-1")
    with pytest.raises(IntegrityError):
        # (budget_id, period_start) does not match any budget_balance row
        await db_conn.execute(
            text(
                "INSERT INTO reservation_line "
                "(reservation_id, budget_id, period_start, amount_micro) "
                "VALUES ('r1', 'b1', TIMESTAMPTZ '2099-01-01 00:00:00+00', 100)"
            )
        )


async def test_reservation_line_accepts_matching_balance_row(db_conn: AsyncConnection) -> None:
    await _seed_reservation_context(db_conn)
    await _insert_reservation(db_conn, reservation_id="r1", idem="idem-1")
    await db_conn.execute(
        text(
            "INSERT INTO reservation_line "
            "(reservation_id, budget_id, period_start, amount_micro) "
            "VALUES ('r1', 'b1', TIMESTAMPTZ '2026-06-01 00:00:00+00', 100)"
        )
    )
