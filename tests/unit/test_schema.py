"""Unit checks that the schema's enum CHECK tuples match the domain enums."""

from __future__ import annotations

from tollgate.adapters.postgres.schema import (
    CREDENTIAL_STATUSES,
    LEDGER_KINDS,
    RESERVATION_STATUSES,
    SCOPE_KINDS,
    metadata,
)
from tollgate.domain.credentials import CredentialStatus
from tollgate.domain.records import LedgerKind
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import ScopeKind


def test_scope_kinds_match_the_domain_enum() -> None:
    assert set(SCOPE_KINDS) == {kind.value for kind in ScopeKind}


def test_reservation_statuses_match_the_domain_enum() -> None:
    assert set(RESERVATION_STATUSES) == {status.value for status in ReservationStatus}


def test_ledger_kinds_match_the_domain_enum() -> None:
    assert set(LEDGER_KINDS) == {kind.value for kind in LedgerKind}


def test_credential_statuses_match_the_domain_enum() -> None:
    assert set(CREDENTIAL_STATUSES) == {status.value for status in CredentialStatus}


def test_metadata_holds_every_expected_table() -> None:
    expected = {
        "org",
        "team",
        "user_principal",
        "project",
        "api_credential",
        "price_book",
        "price",
        "budget",
        "budget_alert",
        "budget_balance",
        "reservation",
        "reservation_line",
        "ledger",
        "idempotency_key",
    }
    assert set(metadata.tables) == expected
