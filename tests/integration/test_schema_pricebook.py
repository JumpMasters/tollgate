"""Constraint tests for the versioned price book (price_book / price)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection


async def test_price_requires_a_published_version(db_conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO price "
                "(price_book_version, provider, model, input_micro_per_token, "
                " output_micro_per_token, cached_input_micro_per_token, "
                " cache_creation_micro_per_token) "
                "VALUES ('v-missing', 'anthropic', 'claude', 2.5, 10, 0.25, 1.25)"
            )
        )


async def test_price_primary_key_rejects_duplicate_triple(db_conn: AsyncConnection) -> None:
    await db_conn.execute(text("INSERT INTO price_book (version) VALUES ('v1')"))
    insert = (
        "INSERT INTO price "
        "(price_book_version, provider, model, input_micro_per_token, "
        " output_micro_per_token, cached_input_micro_per_token, "
        " cache_creation_micro_per_token) "
        "VALUES ('v1', 'anthropic', 'claude', 2.5, 10, 0.25, 1.25)"
    )
    await db_conn.execute(text(insert))
    with pytest.raises(IntegrityError):
        await db_conn.execute(text(insert))


async def test_price_rates_round_trip_as_decimal(db_conn: AsyncConnection) -> None:
    from decimal import Decimal

    await db_conn.execute(text("INSERT INTO price_book (version) VALUES ('v1')"))
    await db_conn.execute(
        text(
            "INSERT INTO price "
            "(price_book_version, provider, model, input_micro_per_token, "
            " output_micro_per_token, cached_input_micro_per_token, "
            " cache_creation_micro_per_token) "
            "VALUES ('v1', 'anthropic', 'claude', 2.5, 10, 0.25, 1.25)"
        )
    )
    row = (
        await db_conn.execute(
            text("SELECT input_micro_per_token FROM price WHERE price_book_version = 'v1'")
        )
    ).scalar_one()
    assert row == Decimal("2.5")


async def test_cache_creation_rate_round_trips_as_decimal(db_conn: AsyncConnection) -> None:
    from decimal import Decimal

    await db_conn.execute(text("INSERT INTO price_book (version) VALUES ('v-cc')"))
    await db_conn.execute(
        text(
            "INSERT INTO price "
            "(price_book_version, provider, model, input_micro_per_token, "
            " output_micro_per_token, cached_input_micro_per_token, "
            " cache_creation_micro_per_token) "
            "VALUES ('v-cc', 'anthropic', 'claude', 1, 2, 0.5, 1.25)"
        )
    )
    row = (
        await db_conn.execute(
            text(
                "SELECT cache_creation_micro_per_token FROM price WHERE price_book_version = 'v-cc'"
            )
        )
    ).scalar_one()
    assert row == Decimal("1.25")
