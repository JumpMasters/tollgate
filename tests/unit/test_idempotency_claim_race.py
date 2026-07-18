"""Unit tests for the claim insert/select race against the key reaper (#70).

``claim`` inserts ``ON CONFLICT DO NOTHING`` then, on conflict, selects the existing row. The
two statements are not atomic, so the reaper can delete the row in between. These tests drive the
interleaving deterministically with a scripted fake connection — an integration test cannot force
a delete into that exact window.
"""

from __future__ import annotations

from typing import Any

import pytest

from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.domain.errors import EnforcementUnavailable
from tollgate.domain.records import ClaimOutcome


class _Row:
    def __init__(self, command_fingerprint: str = "fp", response: Any = None) -> None:
        self.command_fingerprint = command_fingerprint
        self.response = response
        self.key = "k"


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row

    def one_or_none(self) -> Any:
        return self._row


class _ScriptedConn:
    """Returns one scripted row per ``execute`` (statements run insert, select, insert, ...)."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)
        self.calls = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        row = self._rows[self.calls]
        self.calls += 1
        return _Result(row)


def _repo(rows: list[Any]) -> tuple[PostgresIdempotencyRepository, _ScriptedConn]:
    conn = _ScriptedConn(rows)
    return PostgresIdempotencyRepository(conn), conn  # type: ignore[arg-type]


async def test_claim_retries_when_the_key_is_reaped_between_insert_and_select() -> None:
    # attempt 1: insert conflicts (None), select finds nothing (reaped) -> retry;
    # attempt 2: insert succeeds -> FRESH.
    repo, conn = _repo([None, None, _Row()])
    claim = await repo.claim("p1", "k", "fp")
    assert claim.outcome is ClaimOutcome.FRESH
    assert conn.calls == 3


async def test_claim_fails_closed_when_the_race_never_converges() -> None:
    # Every attempt loses the same insert/reap race -> a retryable failure, not a fabricated claim.
    repo, _ = _repo([None] * 6)  # 3 attempts x (insert None, select None)
    with pytest.raises(EnforcementUnavailable):
        await repo.claim("p1", "k", "fp")


async def test_claim_replays_when_the_row_survives_the_select() -> None:
    repo, _ = _repo([None, _Row(response={"reservation_id": "r1"})])
    claim = await repo.claim("p1", "k", "fp")
    assert claim.outcome is ClaimOutcome.REPLAY
    assert claim.response == {"reservation_id": "r1"}


async def test_claim_mismatch_when_the_fingerprint_differs() -> None:
    repo, _ = _repo([None, _Row(command_fingerprint="other")])
    claim = await repo.claim("p1", "k", "fp")
    assert claim.outcome is ClaimOutcome.MISMATCH
