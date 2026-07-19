"""PostgresPriceBookRepository: resolve the current price for a (provider, model) (§3, ADR 0028).

The current price-book version is the one with the latest ``published_at`` (ADR 0028). A single
join from ``price`` to ``price_book``, filtered to the pair and ordered by ``published_at``
descending, takes that version and its rates — or returns ``None`` when the pair is unpriced, which
the reserve turns into an ``UnknownModel`` denial. Explicit async SQLAlchemy Core, no ORM; like the
other repositories it never imports ``application`` and satisfies the port structurally.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import price, price_book
from tollgate.domain.pricing import ModelPrice, PricedModel


class PostgresPriceBookRepository:
    """Current-price resolution on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        """Return the latest-published price for ``(provider, model)``, or ``None`` (ADR 0028)."""
        row = (
            await self._conn.execute(
                select(
                    price.c.price_book_version,
                    price.c.input_micro_per_token,
                    price.c.output_micro_per_token,
                    price.c.cached_input_micro_per_token,
                    price.c.cache_creation_micro_per_token,
                )
                .select_from(
                    price.join(price_book, price.c.price_book_version == price_book.c.version)
                )
                .where(price.c.provider == provider, price.c.model == model)
                .order_by(price_book.c.published_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return PricedModel(
            version=row.price_book_version,
            price=ModelPrice(
                provider=provider,
                model=model,
                input_micro_per_token=Decimal(row.input_micro_per_token),
                output_micro_per_token=Decimal(row.output_micro_per_token),
                cached_input_micro_per_token=Decimal(row.cached_input_micro_per_token),
                cache_creation_micro_per_token=Decimal(row.cache_creation_micro_per_token),
            ),
        )

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        """Return the price stamped at exactly ``version``, or ``None`` if absent (§4).

        A commit reconciles against the reservation's **stamped** version, never the latest;
        the price book is immutable (ADR 0010/0021), so the row either exists exactly as
        published or not at all.
        """
        row = (
            await self._conn.execute(
                select(
                    price.c.input_micro_per_token,
                    price.c.output_micro_per_token,
                    price.c.cached_input_micro_per_token,
                    price.c.cache_creation_micro_per_token,
                ).where(
                    price.c.price_book_version == version,
                    price.c.provider == provider,
                    price.c.model == model,
                )
            )
        ).first()
        if row is None:
            return None
        return ModelPrice(
            provider=provider,
            model=model,
            input_micro_per_token=Decimal(row.input_micro_per_token),
            output_micro_per_token=Decimal(row.output_micro_per_token),
            cached_input_micro_per_token=Decimal(row.cached_input_micro_per_token),
            cache_creation_micro_per_token=Decimal(row.cache_creation_micro_per_token),
        )
