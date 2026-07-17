"""Unit tests for the shared command-handler helpers (§5.0, §5.1, §5.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    command_fingerprint,
    load_owned_reservation,
    ordered_lines,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import ScopeNotAuthorized
from tollgate.domain.ids import (
    BudgetId,
    CredentialId,
    OrgId,
    PrincipalId,
    ReservationId,
    TeamId,
    UserId,
)
from tollgate.domain.records import (
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ScopeKind

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


def _auth(principal_id: str = "u1") -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId(principal_id),
        scope_kind=ScopeKind.USER,
        scope_id=principal_id,
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(user_id=UserId(principal_id), team_id=TeamId("t1"), org_id=OrgId("o1"))
    return AuthContext(credential=credential, principal=principal)


def _record(principal_id: str = "u1") -> ReservationRecord:
    return ReservationRecord(
        reservation_id=ReservationId("res-1"),
        idempotency_key="idem-res",
        principal_id=PrincipalId(principal_id),
        provider="anthropic",
        model="claude",
        price_book_version="2026-06-22",
        estimated_micro=300,
        input_bound_tokens=100,
        max_output_tokens=100,
        ttl_deadline=datetime(2026, 6, 23, 12, 10, tzinfo=UTC),
        labels={"env": "prod"},
    )


class _Reservations:
    """A ReservationRepository fake serving one configured read-back."""

    def __init__(self, stored: StoredReservation | None) -> None:
        self._stored = stored

    async def insert(
        self, reservation: ReservationRecord, lines: Sequence[ReservationLineRecord]
    ) -> None:
        raise AssertionError("not used by these helpers")

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        raise AssertionError("not used by these helpers")

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        return self._stored

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        return ()

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        return False

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        return None


async def test_load_owned_reservation_returns_the_principals_reservation() -> None:
    stored = StoredReservation(record=_record(), status=ReservationStatus.HELD)
    got = await load_owned_reservation(_Reservations(stored), _auth(), ReservationId("res-1"))
    assert got is stored


async def test_unknown_and_foreign_reservations_are_rejected_identically() -> None:
    foreign = StoredReservation(record=_record("intruder"), status=ReservationStatus.HELD)
    with pytest.raises(ScopeNotAuthorized) as unknown_exc:
        await load_owned_reservation(_Reservations(None), _auth(), ReservationId("res-1"))
    with pytest.raises(ScopeNotAuthorized) as foreign_exc:
        await load_owned_reservation(_Reservations(foreign), _auth(), ReservationId("res-1"))
    # the same scope string either way -> no existence leak (§5.0)
    assert unknown_exc.value.scope == foreign_exc.value.scope == "reservation:res-1"


def test_ordered_lines_sorts_into_the_canonical_lock_order() -> None:
    def line(budget_id: str, kind: ScopeKind, scope_id: str) -> ReservationLineView:
        return ReservationLineView(
            node=BudgetNode(BudgetId(budget_id), kind, scope_id),
            period_start=_PERIOD,
            amount_micro=300,
        )

    org = line("b-org", ScopeKind.ORG, "o1")
    user = line("b-user", ScopeKind.USER, "u1")
    project = line("b-proj", ScopeKind.PROJECT, "proj-1")
    assert ordered_lines([project, user, org]) == [org, user, project]


def test_command_fingerprint_is_stable_and_field_sensitive() -> None:
    assert command_fingerprint({"a": 1, "b": 2}) == command_fingerprint({"b": 2, "a": 1})
    assert command_fingerprint({"a": 1}) != command_fingerprint({"a": 2})
    # nested mappings are canonicalized too
    assert command_fingerprint({"labels": {"x": "1", "y": "2"}}) == command_fingerprint(
        {"labels": {"y": "2", "x": "1"}}
    )
