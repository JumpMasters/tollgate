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
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.domain.errors import EnforcementUnavailable
from tollgate.domain.records import ClaimOutcome, IdempotencyClaim

#: Bounded retries for the claim insert/select race against the key reaper (#70). Each retry is
#: only reached when the key vanished between the two statements, which needs a reaper tick to land
#: in that window; more than one such loss in a row is astronomically unlikely.
_MAX_CLAIM_ATTEMPTS: Final = 3


class PostgresIdempotencyRepository:
    """Per-principal idempotency claim/replay on one bound connection (§5.1, #71)."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        """Claim ``(principal_id, key)`` for ``fingerprint`` (§5.1); see the port for outcomes.

        ``INSERT … ON CONFLICT DO NOTHING … RETURNING`` claims the key; on conflict a follow-up
        ``SELECT`` reads the existing row to decide REPLAY vs MISMATCH. The two statements are not
        atomic, so the key reaper can delete the conflicting row in between; a missing row on the
        follow-up means exactly that, and the claim retries the insert (which now succeeds FRESH)
        rather than raising (#70).
        """
        for _attempt in range(_MAX_CLAIM_ATTEMPTS):
            insert_stmt = (
                pg_insert(idempotency_key)
                .values(principal_id=principal_id, key=key, command_fingerprint=fingerprint)
                .on_conflict_do_nothing(index_elements=["principal_id", "key"])
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
                    ).where(
                        idempotency_key.c.principal_id == principal_id,
                        idempotency_key.c.key == key,
                    )
                )
            ).one_or_none()
            if existing is None:
                continue  # reaped between the insert conflict and this select (#70) — retry
            if existing.command_fingerprint != fingerprint:
                return IdempotencyClaim(ClaimOutcome.MISMATCH)
            return IdempotencyClaim(ClaimOutcome.REPLAY, response=existing.response)

        # Every attempt lost the same insert/reap race — treat as a transient failure to decide,
        # which is fail-closed and retryable, rather than fabricating a claim outcome.
        raise EnforcementUnavailable("idempotency claim did not converge under reaper contention")

    async def store_response(
        self, principal_id: str, key: str, status: str, response: Mapping[str, Any]
    ) -> None:
        """Cache a command's response on its key row so a later duplicate replays it."""
        stmt = (
            update(idempotency_key)
            .where(
                idempotency_key.c.principal_id == principal_id,
                idempotency_key.c.key == key,
            )
            .values(status=status, response=dict(response))
        )
        await self._conn.execute(stmt)

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        """Delete up to ``limit`` keys created before ``cutoff``; return the count removed (§5.5).

        Bounded so the reaper never issues one unbounded delete — the caller loops until a batch
        comes back short. Keys are addressed via a ``(principal_id, key)`` primary-key sub-select
        so the ``LIMIT`` applies to the delete set (Postgres has no ``DELETE … LIMIT``). Safe
        because ``reservation``'s idempotency guard is UNIQUE, not a foreign key: an aged key never
        orphans a reservation, and a later reuse that collides with the surviving reservation row is
        mapped to a 409 on the reserve path (#61). The sub-select filters and orders by
        ``created_at`` — served by ``ix_idempotency_key_created_at`` so each batch is an index range
        scan, not a full table scan (#63) — and takes its locks with ``FOR UPDATE SKIP LOCKED``, so
        concurrent reapers pick disjoint oldest-first batches and make progress without deadlocking.
        """
        picked = (
            select(idempotency_key.c.principal_id, idempotency_key.c.key)
            .where(idempotency_key.c.created_at < cutoff)
            .order_by(idempotency_key.c.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._conn.execute(
            delete(idempotency_key).where(
                tuple_(idempotency_key.c.principal_id, idempotency_key.c.key).in_(picked)
            )
        )
        return result.rowcount
