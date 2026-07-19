"""Tests for the persistence-port value types added for the terminal commands (plan 10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tollgate.domain.ids import BudgetId, PrincipalId, ReservationId
from tollgate.domain.records import (
    ClaimOutcome,
    IdempotencyClaim,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ScopeKind

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


def _record() -> ReservationRecord:
    return ReservationRecord(
        reservation_id=ReservationId("res-1"),
        idempotency_key="idem-1",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="pb-1",
        estimated_micro=300,
        input_bound_tokens=100,
        max_output_tokens=100,
        ttl_deadline=datetime(2026, 6, 23, 12, 10, tzinfo=UTC),
        labels={"env": "prod"},
    )


def test_stored_reservation_pairs_the_row_with_its_live_status() -> None:
    stored = StoredReservation(record=_record(), status=ReservationStatus.REAPED)
    assert stored.record.estimated_micro == 300
    assert stored.status is ReservationStatus.REAPED


def test_reservation_line_view_carries_the_budget_node_for_lock_ordering() -> None:
    view = ReservationLineView(
        node=BudgetNode(BudgetId("b-user"), ScopeKind.USER, "u1"),
        period_start=_PERIOD,
        amount_micro=300,
    )
    assert view.node.scope_kind is ScopeKind.USER
    assert view.period_start == _PERIOD
    assert view.amount_micro == 300


def test_reservation_record_labels_are_an_immutable_copy() -> None:
    source = {"team": "blue"}
    record = ReservationRecord(
        reservation_id=ReservationId("res-1"),
        idempotency_key="idem-1",
        principal_id=PrincipalId("u1"),
        provider="anthropic",
        model="claude",
        price_book_version="pb-1",
        estimated_micro=300,
        input_bound_tokens=100,
        max_output_tokens=100,
        ttl_deadline=_PERIOD,
        labels=source,
    )
    source["team"] = "red"
    assert record.labels == {"team": "blue"}  # a copy, not the caller's dict (#78)
    with pytest.raises(TypeError):
        record.labels["team"] = "x"  # type: ignore[index]  # read-only view


def test_idempotency_claim_response_is_an_immutable_copy() -> None:
    source = {"reservation_id": "r1"}
    claim = IdempotencyClaim(ClaimOutcome.REPLAY, response=source)
    source["reservation_id"] = "r2"
    assert claim.response == {"reservation_id": "r1"}  # a copy of the cached response (#78)
    assert claim.response is not None
    with pytest.raises(TypeError):
        claim.response["reservation_id"] = "x"  # type: ignore[index]  # read-only view


def test_idempotency_claim_without_a_response_stays_none() -> None:
    assert IdempotencyClaim(ClaimOutcome.FRESH).response is None
