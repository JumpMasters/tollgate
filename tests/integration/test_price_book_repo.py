"""Integration tests for PostgresPriceBookRepository (real Postgres, ADR 0028)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.price_book_repo import PostgresPriceBookRepository
from tollgate.adapters.postgres.schema import price, price_book


async def _publish(
    conn: AsyncConnection,
    *,
    version: str,
    published_at: datetime,
    input_rate: str,
    output_rate: str,
    cached_rate: str,
    cache_creation_rate: str = "1.25",
    provider: str = "anthropic",
    model: str = "claude",
) -> None:
    await conn.execute(price_book.insert().values(version=version, published_at=published_at))
    await conn.execute(
        price.insert().values(
            price_book_version=version,
            provider=provider,
            model=model,
            input_micro_per_token=Decimal(input_rate),
            output_micro_per_token=Decimal(output_rate),
            cached_input_micro_per_token=Decimal(cached_rate),
            cache_creation_micro_per_token=Decimal(cache_creation_rate),
        )
    )


async def test_resolve_price_takes_the_latest_published_version(db_conn: AsyncConnection) -> None:
    await _publish(
        db_conn,
        version="v1",
        published_at=datetime(2026, 5, 1, tzinfo=UTC),
        input_rate="1",
        output_rate="2",
        cached_rate="0.5",
    )
    await _publish(
        db_conn,
        version="v2",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_rate="3",
        output_rate="4",
        cached_rate="1.5",
    )
    priced = await PostgresPriceBookRepository(db_conn).resolve_price("anthropic", "claude")
    assert priced is not None
    assert priced.version == "v2"
    assert priced.price.input_micro_per_token == Decimal("3")
    assert priced.price.output_micro_per_token == Decimal("4")
    assert priced.price.cached_input_micro_per_token == Decimal("1.5")


async def test_resolve_price_breaks_a_published_at_tie_deterministically(
    db_conn: AsyncConnection,
) -> None:
    # published_at defaults to the transaction timestamp, so a bulk seed publishes several versions
    # with the SAME published_at. Ordering by published_at alone then leaves an arbitrary pick that
    # can differ run to run; version is the deterministic tiebreak so "latest published" is a total
    # order (#98). The stamped version drives commit reconciliation, so it must not be ambiguous.
    same_instant = datetime(2026, 6, 1, tzinfo=UTC)
    await _publish(
        db_conn,
        version="v-a",
        published_at=same_instant,
        input_rate="1",
        output_rate="2",
        cached_rate="0.5",
    )
    await _publish(
        db_conn,
        version="v-b",
        published_at=same_instant,
        input_rate="3",
        output_rate="4",
        cached_rate="1.5",
    )
    priced = await PostgresPriceBookRepository(db_conn).resolve_price("anthropic", "claude")
    assert priced is not None
    assert priced.version == "v-b"  # highest version wins the tie, deterministically
    assert priced.price.input_micro_per_token == Decimal("3")


async def test_resolve_price_returns_none_for_an_unpriced_pair(db_conn: AsyncConnection) -> None:
    await _publish(
        db_conn,
        version="v1",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_rate="1",
        output_rate="2",
        cached_rate="0.5",
    )
    assert await PostgresPriceBookRepository(db_conn).resolve_price("openai", "gpt") is None


async def test_resolve_price_includes_cache_creation_rate(db_conn: AsyncConnection) -> None:
    await _publish(
        db_conn,
        version="v1",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_rate="1",
        output_rate="2",
        cached_rate="0.5",
        cache_creation_rate="1.25",
    )
    resolved = await PostgresPriceBookRepository(db_conn).resolve_price("anthropic", "claude")
    assert resolved is not None
    assert resolved.price.cache_creation_micro_per_token == Decimal("1.25")


async def test_db_price_prices_cache_creation_end_to_end(db_conn: AsyncConnection) -> None:
    from decimal import Decimal

    from tollgate.adapters.postgres.price_book_repo import PostgresPriceBookRepository
    from tollgate.adapters.postgres.schema import price, price_book
    from tollgate.domain.pricing import actual_micro

    await db_conn.execute(price_book.insert().values(version="pb-e2e"))
    await db_conn.execute(
        price.insert().values(
            price_book_version="pb-e2e",
            provider="anthropic",
            model="claude",
            input_micro_per_token=Decimal("1"),
            output_micro_per_token=Decimal("2"),
            cached_input_micro_per_token=Decimal("0.5"),
            cache_creation_micro_per_token=Decimal("1.25"),  # premium: above input=1
        )
    )
    resolved = await PostgresPriceBookRepository(db_conn).resolve_price("anthropic", "claude")
    assert resolved is not None
    # 100 in / 20 cache-read / 50 out, plus 40 disjoint cache-creation tokens:
    #   (100-20)*1 + 20*0.5 + 50*2 + 40*1.25 = 80 + 10 + 100 + 50 = 240
    cost = actual_micro(
        resolved.price,
        input_tokens=100,
        output_tokens=50,
        cached_input_tokens=20,
        cache_creation_tokens=40,
    )
    assert cost == 240
    # and the same call with no creation tokens is 50 micro cheaper (proves the term is additive):
    assert (
        actual_micro(resolved.price, input_tokens=100, output_tokens=50, cached_input_tokens=20)
        == 190
    )
