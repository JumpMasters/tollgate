"""Integration tests for PostgresIdempotencyRepository (real Postgres, §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.domain.records import ClaimOutcome

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


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


async def _insert_key(db_conn: AsyncConnection, *, key: str, created_at: datetime) -> None:
    await db_conn.execute(
        idempotency_key.insert().values(key=key, command_fingerprint="fp", created_at=created_at)
    )


async def test_delete_expired_removes_only_aged_keys_up_to_limit(db_conn: AsyncConnection) -> None:
    cutoff = _NOW - timedelta(hours=24)
    await _insert_key(db_conn, key="old-1", created_at=cutoff - timedelta(hours=1))
    await _insert_key(db_conn, key="old-2", created_at=cutoff - timedelta(hours=2))
    await _insert_key(db_conn, key="old-3", created_at=cutoff - timedelta(hours=3))
    await _insert_key(db_conn, key="fresh", created_at=cutoff + timedelta(minutes=1))
    repo = PostgresIdempotencyRepository(db_conn)

    removed = await repo.delete_expired(cutoff, limit=2)
    assert removed == 2  # bounded by the limit

    removed_again = await repo.delete_expired(cutoff, limit=2)
    assert removed_again == 1  # only the third aged key remains; short batch

    survivors = {row.key for row in (await db_conn.execute(idempotency_key.select())).all()}
    assert survivors == {"fresh"}  # the fresh key is never touched
