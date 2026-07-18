"""Tests for UUIDv7 identifier generation."""

from __future__ import annotations

from tollgate.adapters.postgres.identifiers import (
    Uuid7IdGenerator,
    new_ledger_entry_id,
    new_reservation_id,
)


def test_ledger_ids_are_unique_strings() -> None:
    first = new_ledger_entry_id()
    second = new_ledger_entry_id()
    assert isinstance(first, str)
    assert first != second


def test_reservation_ids_are_unique() -> None:
    assert new_reservation_id() != new_reservation_id()


def test_uuid7_id_generator_mints_distinct_ids() -> None:
    gen = Uuid7IdGenerator()
    assert gen.new_reservation_id() != gen.new_reservation_id()
    assert gen.new_ledger_entry_id() != gen.new_ledger_entry_id()
    # each is a 36-char uuid string
    assert len(gen.new_reservation_id()) == 36
    assert len(gen.new_ledger_entry_id()) == 36
