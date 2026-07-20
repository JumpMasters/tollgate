"""PostgresLedgerRepository: append-only writes to the ``ledger`` (§5.2).

The ledger is the immutable audit trail; this repository only ever inserts and is never
summed on the command path — the offline conservation oracle (plan 16, ADR 0011) is the sole
reader that aggregates it. ``append`` writes one row per entry in a single executemany.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import ledger
from tollgate.domain.records import LedgerEntry


class PostgresLedgerRepository:
    """Append rows to the audit ledger on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        """Append one row per entry in the current transaction (``ts`` is server-defaulted)."""
        if not entries:
            return
        await self._conn.execute(
            insert(ledger),
            [
                {
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "budget_id": entry.budget_id,
                    "period_start": entry.period_start,
                    "reservation_id": entry.reservation_id,
                    "delta_reserved_micro": entry.delta_reserved_micro,
                    "delta_committed_micro": entry.delta_committed_micro,
                    "delta_overage_micro": entry.delta_overage_micro,
                    "actual_input_tokens": entry.actual_input_tokens,
                    "actual_output_tokens": entry.actual_output_tokens,
                    "provider": entry.provider,
                    "price_book_version": entry.price_book_version,
                    "ref": entry.ref,
                    "model": entry.model,
                    "labels": None if entry.labels is None else dict(entry.labels),
                }
                for entry in entries
            ],
        )
