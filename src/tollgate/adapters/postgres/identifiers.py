"""Identifier generation for ledger and reservation rows.

Ledger entries use UUIDv7 so that primary keys are time-ordered, which keeps the
append-only inserts index-friendly.
"""

from __future__ import annotations

import uuid_utils

from tollgate.domain.ids import LedgerEntryId, ReservationId


def new_ledger_entry_id() -> LedgerEntryId:
    """Return a fresh time-ordered (UUIDv7) ledger entry id."""
    return LedgerEntryId(str(uuid_utils.uuid7()))


def new_reservation_id() -> ReservationId:
    """Return a fresh time-ordered (UUIDv7) reservation id."""
    return ReservationId(str(uuid_utils.uuid7()))
