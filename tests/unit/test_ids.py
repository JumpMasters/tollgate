"""Tests for the typed identifiers."""

from __future__ import annotations

from tollgate.domain.ids import (
    BudgetId,
    CredentialId,
    LedgerEntryId,
    OrgId,
    PrincipalId,
    ProjectId,
    ReservationId,
    TeamId,
    UserId,
)


def test_newtypes_wrap_their_value() -> None:
    assert OrgId("o1") == "o1"
    assert TeamId("t1") == "t1"
    assert UserId("u1") == "u1"
    assert ProjectId("p1") == "p1"
    assert BudgetId("b1") == "b1"
    assert ReservationId("r1") == "r1"
    assert PrincipalId("pr1") == "pr1"
    assert LedgerEntryId("l1") == "l1"
    assert CredentialId("c1") == "c1"
