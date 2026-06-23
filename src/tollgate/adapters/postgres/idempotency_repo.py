"""PostgresIdempotencyRepository: claim/replay over ``idempotency_key`` (§5.1).

The unique primary key serializes duplicate commands. ``claim`` inserts the key with
``ON CONFLICT (key) DO NOTHING … RETURNING``: a returned row means this caller claimed it
(``FRESH``); no row means the key already exists, so the stored response is replayed
(``REPLAY``) unless the command fingerprint differs (``MISMATCH`` = key reuse).
``store_response`` caches the outcome on the key row at the end of the command transaction.
Cross-transaction serialization of concurrent duplicates is exercised in plan 07.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.domain.records import ClaimOutcome, IdempotencyClaim


class PostgresIdempotencyRepository:
    """Idempotency claim/replay on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        """Claim ``key`` for ``fingerprint`` (§5.1); see the class docstring for outcomes."""
        insert_stmt = (
            pg_insert(idempotency_key)
            .values(key=key, command_fingerprint=fingerprint)
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(idempotency_key.c.key)
        )
        claimed = (await self._conn.execute(insert_stmt)).first()
        if claimed is not None:
            return IdempotencyClaim(ClaimOutcome.FRESH)

        existing = (
            await self._conn.execute(
                select(
                    idempotency_key.c.command_fingerprint,
                    idempotency_key.c.response,
                ).where(idempotency_key.c.key == key)
            )
        ).one()
        if existing.command_fingerprint != fingerprint:
            return IdempotencyClaim(ClaimOutcome.MISMATCH)
        return IdempotencyClaim(ClaimOutcome.REPLAY, response=existing.response)

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        """Cache a command's response on its key row so a later duplicate replays it."""
        stmt = (
            update(idempotency_key)
            .where(idempotency_key.c.key == key)
            .values(status=status, response=dict(response))
        )
        await self._conn.execute(stmt)
