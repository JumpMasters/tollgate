"""Tests for UUIDv7 identifier generation."""

from __future__ import annotations

from tollgate.adapters.postgres.identifiers import new_ledger_entry_id, new_reservation_id


def test_ledger_ids_are_unique_strings() -> None:
    first = new_ledger_entry_id()
    second = new_ledger_entry_id()
    assert isinstance(first, str)
    assert first != second


def test_reservation_ids_are_unique() -> None:
    assert new_reservation_id() != new_reservation_id()
