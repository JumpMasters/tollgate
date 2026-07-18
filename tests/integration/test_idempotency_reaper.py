"""End-to-end: the idempotency reaper deletes only aged keys, in bounded batches (§5.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.schema import idempotency_key
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.handlers.reap import IdempotencyReaperHandler

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


class _ClockAt:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def _insert_key(
    engine: AsyncEngine, *, key: str, created_at: datetime, principal_id: str = "p1"
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            idempotency_key.insert().values(
                principal_id=principal_id,
                key=key,
                command_fingerprint="fp",
                created_at=created_at,
            )
        )


async def _count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM idempotency_key"))).scalar_one())


async def test_idempotency_reaper_deletes_only_aged_keys_in_bounded_batches(
    committing_engine: AsyncEngine,
) -> None:
    old = _NOW - timedelta(hours=25)
    fresh = _NOW - timedelta(hours=1)
    for i in range(5):
        await _insert_key(committing_engine, key=f"old-{i}", created_at=old)
    for i in range(3):
        await _insert_key(committing_engine, key=f"fresh-{i}", created_at=fresh)

    handler = IdempotencyReaperHandler(
        uow=PostgresUnitOfWork(committing_engine),
        clock=_ClockAt(_NOW),
        ttl_hours=24,
        batch_size=2,  # forces multiple bounded batches: 2 + 2 + 1
    )
    deleted = await handler.run_once()
    assert deleted == 5  # all five aged keys, across three batches
    assert await _count(committing_engine) == 3  # the three fresh keys survive
    async with committing_engine.connect() as conn:
        remaining = (await conn.execute(text("SELECT key FROM idempotency_key"))).scalars().all()
    assert set(remaining) == {"fresh-0", "fresh-1", "fresh-2"}


async def test_idempotency_reaper_with_no_aged_keys_deletes_nothing(
    committing_engine: AsyncEngine,
) -> None:
    await _insert_key(committing_engine, key="fresh", created_at=_NOW - timedelta(hours=1))
    handler = IdempotencyReaperHandler(
        uow=PostgresUnitOfWork(committing_engine), clock=_ClockAt(_NOW), ttl_hours=24, batch_size=2
    )
    assert await handler.run_once() == 0
    assert await _count(committing_engine) == 1
