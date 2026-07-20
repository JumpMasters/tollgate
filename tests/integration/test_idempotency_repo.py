"""Integration tests for PostgresIdempotencyRepository (real Postgres, §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.domain.errors import TollgateError
from tollgate.domain.records import ClaimOutcome

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


async def test_claim_fresh_key_returns_fresh(db_conn: AsyncConnection) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    claim = await repo.claim("p1", "k1", "fp-1")
    assert claim.outcome is ClaimOutcome.FRESH
    assert claim.response is None


async def test_claim_same_key_same_fingerprint_replays_stored_response(
    db_conn: AsyncConnection,
) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("p1", "k1", "fp-1")
    await repo.store_response("p1", "k1", {"reservation_id": "r1"})
    claim = await repo.claim("p1", "k1", "fp-1")
    assert claim.outcome is ClaimOutcome.REPLAY
    assert claim.response == {"reservation_id": "r1"}


async def test_store_response_on_an_unclaimed_key_fails_loud(db_conn: AsyncConnection) -> None:
    # store_response only ever runs after claim inserted the row in the same transaction, so it
    # must match exactly one row; a rowcount of zero is an invariant breach, not a client outcome,
    # and raising beats silently dropping the response and leaving a keyless success (#107).
    repo = PostgresIdempotencyRepository(db_conn)
    with pytest.raises(TollgateError):
        await repo.store_response("p1", "never-claimed", {"x": 1})


async def test_claim_same_key_different_fingerprint_is_mismatch(db_conn: AsyncConnection) -> None:
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("p1", "k1", "fp-1")
    claim = await repo.claim("p1", "k1", "fp-2")
    assert claim.outcome is ClaimOutcome.MISMATCH
    assert claim.response is None


async def test_same_key_under_different_principals_do_not_collide(db_conn: AsyncConnection) -> None:
    # Keys are scoped per principal, so two tenants that choose the same key string each claim
    # it FRESH — a global namespace would give the second a spurious MISMATCH (#71).
    repo = PostgresIdempotencyRepository(db_conn)
    first = await repo.claim("p1", "shared-key", "fp-p1")
    second = await repo.claim("p2", "shared-key", "fp-p2")
    assert first.outcome is ClaimOutcome.FRESH
    assert second.outcome is ClaimOutcome.FRESH


async def test_duplicate_claim_before_response_replays_null(db_conn: AsyncConnection) -> None:
    # A duplicate that arrives after the key is claimed but before its response is stored
    # replays a NULL response. (Cross-transaction serialization — a real duplicate blocks on
    # the unique index until the first commits — is exercised in plan 07; here both claims run
    # on one connection.)
    repo = PostgresIdempotencyRepository(db_conn)
    await repo.claim("p1", "k1", "fp-1")
    claim = await repo.claim("p1", "k1", "fp-1")
    assert claim.outcome is ClaimOutcome.REPLAY
    assert claim.response is None


async def _insert_key(
    db_conn: AsyncConnection, *, key: str, created_at: datetime, principal_id: str = "p1"
) -> None:
    await db_conn.execute(
        idempotency_key.insert().values(
            principal_id=principal_id,
            key=key,
            command_fingerprint="fp",
            created_at=created_at,
        )
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
