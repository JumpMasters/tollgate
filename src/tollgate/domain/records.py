"""Value types exchanged across the persistence ports (plan 06).

These are the immutable rows the repositories write — a reservation and its lines, a ledger
entry — plus the outcome of an idempotency-key claim. They carry no behaviour and no I/O:
the adapters translate them to SQL; the application constructs them. They live in ``domain``
(the pure leaf) so both the ports (``application``) and the adapters can name them without
crossing an import boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from tollgate.domain.ids import BudgetId, LedgerEntryId, PrincipalId, ReservationId
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode


class LedgerKind(StrEnum):
    """The kind of an append-only ledger entry (mirrors the schema's CHECK)."""

    RESERVE = "reserve"
    COMMIT_ADJUST = "commit_adjust"
    RELEASE = "release"
    REAP = "reap"
    OVERAGE = "overage"
    GRACE_BACKFILL = "grace_backfill"


class ClaimOutcome(StrEnum):
    """The result of claiming an idempotency key (§5.1)."""

    FRESH = "fresh"  # claimed now — this caller owns the effect
    REPLAY = "replay"  # the key already completed — return the stored response
    MISMATCH = "mismatch"  # the key exists under a different command fingerprint (reuse)


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    """A held reservation row to persist on a successful reserve (§5.2).

    ``status`` is not carried — a fresh reservation is always ``held`` (the column defaults to
    it) — and ``created_at`` is server-defaulted. ``labels`` are opaque chargeback tags stored
    as JSONB.
    """

    reservation_id: ReservationId
    idempotency_key: str
    principal_id: PrincipalId
    provider: str
    model: str
    price_book_version: str
    estimated_micro: int
    input_bound_tokens: int
    max_output_tokens: int
    ttl_deadline: datetime
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ReservationLineRecord:
    """One node's share of a reservation — the audit trail of which budgets it held."""

    reservation_id: ReservationId
    budget_id: BudgetId
    period_start: datetime
    amount_micro: int


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One append-only ledger row (§5.2).

    The three deltas default to zero. The token counts are recorded on commit/overage
    entries and left ``None`` elsewhere; ``provider`` and ``price_book_version`` are also
    stamped on reserve entries, pinning the cost basis the estimate was priced against.
    A single call's ``commit_adjust`` and ``overage`` rows describe the same call and so
    carry the SAME token counts — summing token columns across a reservation's rows
    double-counts; only the monetary deltas are additive across rows. ``reservation_id``
    is nullable so a ``grace_backfill`` entry (which has no live reservation) can still be
    recorded.
    """

    entry_id: LedgerEntryId
    kind: LedgerKind
    budget_id: BudgetId
    period_start: datetime
    reservation_id: ReservationId | None = None
    delta_reserved_micro: int = 0
    delta_committed_micro: int = 0
    delta_overage_micro: int = 0
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    provider: str | None = None
    price_book_version: str | None = None
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class StoredReservation:
    """A persisted reservation read back for a lifecycle command (§5.2, §5.4).

    ``record`` is the immutable insert-time row; ``status`` is the live lifecycle state the
    identity guards branch on — held routes to the normal terminal path, reaped to the
    self-healing late commit (ADR 0029), anything else to :class:`ReservationNotHeld`.
    """

    record: ReservationRecord
    status: ReservationStatus


@dataclass(frozen=True, slots=True)
class ReservationLineView:
    """A reservation line joined with the budget node it drew on (§5.3, §5.4).

    Terminal commands update the same ``budget_balance`` rows concurrent reserves contend on;
    carrying the full :class:`BudgetNode` (not just the ``budget_id``) lets them walk the lines
    in the canonical ``lock_order_key`` order the reserve used, so the two can never form a
    lock cycle on shared parent rows.
    """

    node: BudgetNode
    period_start: datetime
    amount_micro: int


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The outcome of an idempotency-key claim; ``response`` is set only on ``REPLAY``."""

    outcome: ClaimOutcome
    response: Mapping[str, Any] | None = None
