"""Cross-transaction idempotency dedup under contention (real Postgres, §5.1).

Plan 06 proved claim/replay on one connection; this proves the serialization the spec relies on:
two concurrent duplicates race the unique PK, one claims it FRESH and the other blocks on the
index until the first commits, then REPLAYS the stored response — one effect, never two.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.domain.records import ClaimOutcome


async def _claim_once(engine: AsyncEngine, key: str, fingerprint: str) -> ClaimOutcome:
    """Claim the key in its own transaction; only the FRESH winner stores a response."""
    async with engine.connect() as conn:
        txn = await conn.begin()
        repo = PostgresIdempotencyRepository(conn)
        claim = await repo.claim(key, fingerprint)
        if claim.outcome is ClaimOutcome.FRESH:
            await repo.store_response(key, "succeeded", {"reservation_id": "r1"})
        await txn.commit()
        return claim.outcome


async def test_concurrent_duplicate_claims_dedup_to_one_effect(
    committing_engine: AsyncEngine,
) -> None:
    outcomes = await asyncio.gather(
        _claim_once(committing_engine, "dup-key", "fp-1"),
        _claim_once(committing_engine, "dup-key", "fp-1"),
    )
    # Exactly one claims FRESH; the other blocks on the unique index until the first commits,
    # then finds the conflict and REPLAYS the stored response.
    assert Counter(outcomes) == {ClaimOutcome.FRESH: 1, ClaimOutcome.REPLAY: 1}
    async with committing_engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT response FROM idempotency_key WHERE key = 'dup-key'"))
        ).all()
    assert len(rows) == 1  # one effect, not two
    assert rows[0].response == {"reservation_id": "r1"}
