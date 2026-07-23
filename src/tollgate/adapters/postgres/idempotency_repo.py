"""PostgresIdempotencyRepository: claim/replay over a per-principal idempotency table.

The composite ``(principal_id, key)`` primary key serializes duplicate commands per principal (#71).
``claim`` inserts the row with ``ON CONFLICT (principal_id, key) DO NOTHING … RETURNING``: a
returned row means this caller claimed it (``FRESH``); no row means the key already exists, so the
stored response is replayed (``REPLAY``) unless the command fingerprint differs (``MISMATCH`` = key
reuse). ``store_response`` caches the outcome on that row at the end of the command transaction.
Cross-transaction serialization of concurrent duplicates is exercised separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Table, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.domain.errors import EnforcementUnavailable, TollgateError
from tollgate.domain.records import ClaimOutcome, IdempotencyClaim

#: Bounded retries for the claim insert/select race against the key reaper (#70). Each retry is
#: only reached when the key vanished between the two statements, which needs a reaper tick to land
#: in that window; more than one such loss in a row is astronomically unlikely.
_MAX_CLAIM_ATTEMPTS: Final = 3


class PostgresIdempotencyRepository:
    """Per-principal idempotency claim/replay on one bound connection (#71).

    The target table is injectable so the same claim/replay/mismatch mechanism serves both the
    reaped ``idempotency_key`` table (reserve/commit/cancel, whose durable dedup lives elsewhere)
    and the never-reaped ``metered_receipt`` table (meter/grace, which have no other backstop, #92).
    """

    def __init__(self, conn: AsyncConnection, table: Table = idempotency_key) -> None:
        self._conn = conn
        self._table = table

    async def claim(self, principal_id: str, key: str, fingerprint: str) -> IdempotencyClaim:
        """Claim ``(principal_id, key)`` for ``fingerprint``; see the port for outcomes.

        ``INSERT … ON CONFLICT DO NOTHING … RETURNING`` claims the key; on conflict a follow-up
        ``SELECT`` reads the existing row to decide REPLAY vs MISMATCH. The two statements are not
        atomic, so the key reaper can delete the conflicting row in between; a missing row on the
        follow-up means exactly that, and the claim retries the insert (which now succeeds FRESH)
        rather than raising (#70).
        """
        for _attempt in range(_MAX_CLAIM_ATTEMPTS):
            insert_stmt = (
                pg_insert(self._table)
                .values(principal_id=principal_id, key=key, command_fingerprint=fingerprint)
                .on_conflict_do_nothing(index_elements=["principal_id", "key"])
                .returning(self._table.c.key)
            )
            claimed = (await self._conn.execute(insert_stmt)).first()
            if claimed is not None:
                return IdempotencyClaim(ClaimOutcome.FRESH)

            existing = (
                await self._conn.execute(
                    select(
                        self._table.c.command_fingerprint,
                        self._table.c.response,
                    ).where(
                        self._table.c.principal_id == principal_id,
                        self._table.c.key == key,
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
        self, principal_id: str, key: str, response: Mapping[str, Any]
    ) -> None:
        """Cache a command's response on its key row so a later duplicate replays it.

        Guarded: ``store_response`` only ever runs after ``claim`` inserted this row in the same
        transaction, so it must match exactly one row. A ``rowcount`` other than one means the row
        vanished under an invariant breach, not a client outcome; fail loud so the transaction rolls
        back rather than silently dropping the response and leaving a keyless success behind (#107).
        """
        stmt = (
            update(self._table)
            .where(
                self._table.c.principal_id == principal_id,
                self._table.c.key == key,
            )
            .values(response=dict(response))
        )
        result = await self._conn.execute(stmt)
        if result.rowcount != 1:
            raise TollgateError(f"idempotency store_response matched {result.rowcount} rows, not 1")

    async def delete_expired(self, cutoff: datetime, limit: int) -> int:
        """Delete up to ``limit`` keys created before ``cutoff``; return the count removed.

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
            select(self._table.c.principal_id, self._table.c.key)
            .where(self._table.c.created_at < cutoff)
            .order_by(self._table.c.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._conn.execute(
            delete(self._table).where(
                tuple_(self._table.c.principal_id, self._table.c.key).in_(picked)
            )
        )
        return result.rowcount
