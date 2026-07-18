"""Unit tests for reservation-insert error translation and the empty-lines guard (#61)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from tollgate.adapters.postgres.reservations_repo import (
    PostgresReservationRepository,
    _violated_constraint,
)
from tollgate.domain.errors import IdempotencyKeyReuse
from tollgate.domain.ids import PrincipalId, ReservationId
from tollgate.domain.records import ReservationRecord

_IDEMPOTENCY_UNIQUE = "uq_reservation_principal_id_idempotency_key"


class _DriverError(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


def _integrity_error(constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT INTO reservation ...", {}, _DriverError(constraint_name))


class _RaisingConn:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._exc


class _OkConn:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        return None


def _record() -> ReservationRecord:
    return ReservationRecord(
        reservation_id=ReservationId("r1"),
        idempotency_key="idem-1",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="v1",
        estimated_micro=100,
        input_bound_tokens=50,
        max_output_tokens=50,
        ttl_deadline=datetime(2026, 6, 1, tzinfo=UTC),
        labels={"team": "blue"},
    )


def test_violated_constraint_reads_the_driver_constraint_name() -> None:
    assert _violated_constraint(_integrity_error(_IDEMPOTENCY_UNIQUE)) == _IDEMPOTENCY_UNIQUE


def test_violated_constraint_is_none_when_the_name_is_unavailable() -> None:
    assert _violated_constraint(_integrity_error(None)) is None


async def test_insert_maps_the_idempotency_unique_to_key_reuse() -> None:
    repo = PostgresReservationRepository(_RaisingConn(_integrity_error(_IDEMPOTENCY_UNIQUE)))  # type: ignore[arg-type]
    with pytest.raises(IdempotencyKeyReuse):
        await repo.insert(_record(), [])


async def test_insert_reraises_a_non_idempotency_integrity_error() -> None:
    # An FK or other integrity failure must NOT be mislabeled as key reuse; it propagates as-is.
    other = _integrity_error("fk_reservation_principal_id_user_principal")
    repo = PostgresReservationRepository(_RaisingConn(other))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.insert(_record(), [])


async def test_insert_with_no_lines_skips_the_line_write() -> None:
    conn = _OkConn()
    repo = PostgresReservationRepository(conn)  # type: ignore[arg-type]
    await repo.insert(_record(), [])
    assert conn.calls == 1  # only the reservation row; the empty-lines guard skips the second write
