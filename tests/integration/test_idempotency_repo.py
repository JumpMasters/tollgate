"""Integration tests for PostgresIdempotencyRepository (real Postgres, §5.1)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.domain.records import ClaimOutcome


async def test_claim_fresh_key_returns_fresh(db_conn: AsyncConnection) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    claim = await repo.claim("k1", "fp-1")
    assert claim.outcome is ClaimOutcome.FRESH
    assert claim.response is None


async def test_claim_same_key_same_fingerprint_replays_stored_response(
    db_conn: AsyncConnection,
) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("k1", "fp-1")
    await repo.store_response("k1", "succeeded", {"reservation_id": "r1"})
    claim = await repo.claim("k1", "fp-1")
    assert claim.outcome is ClaimOutcome.REPLAY
    assert claim.response == {"reservation_id": "r1"}


async def test_claim_same_key_different_fingerprint_is_mismatch(db_conn: AsyncConnection) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("k1", "fp-1")
    claim = await repo.claim("k1", "fp-2")
    assert claim.outcome is ClaimOutcome.MISMATCH
    assert claim.response is None


async def test_duplicate_claim_before_response_replays_null(db_conn: AsyncConnection) -> None:
    # A duplicate that arrives after the key is claimed but before its response is stored
    # replays a NULL response. (Cross-transaction serialization — a real duplicate blocks on
    # the unique index until the first commits — is exercised in plan 07; here both claims run
    # on one connection.)
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("k1", "fp-1")
    claim = await repo.claim("k1", "fp-1")
    assert claim.outcome is ClaimOutcome.REPLAY
    assert claim.response is None
