"""Command and result value types for the reservation commands and the grace backfill (§4, §5).

These are the pure, immutable request/outcome shapes shared by the application
handlers (which orchestrate the transaction), the HTTP surface (which (de)serializes
them), and the correctness harness (which drives them). They carry no behaviour and
no I/O: authentication, estimation, persistence, and reconciliation all happen in the
layers above. The acting principal is *derived from the credential* by the
application (§5.0) and is therefore not a field on any command -- a caller cannot
assert an identity here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from tollgate.domain.ids import ProjectId, ReservationId


@dataclass(frozen=True, slots=True)
class ReserveCommand:
    """A request to reserve worst-case budget before a model call (§4).

    ``input_bound_tokens`` is the tokenizer-derived upper bound on the prompt and
    ``max_output_tokens`` the provider ceiling; together they drive the worst-case
    estimate the cost model computes (plan 01). ``project_id`` is set only when the
    request named a project *and* the credential authorizes it (resolved in the
    application). ``labels`` are opaque chargeback tags carried onto the reservation.
    """

    idempotency_key: str
    provider: str
    model: str
    input_bound_tokens: int
    max_output_tokens: int
    labels: Mapping[str, str]
    project_id: ProjectId | None = None

    def __post_init__(self) -> None:
        # frozen guards rebinding, not the referenced dict; store a read-only copy so a later
        # mutation of the caller's dict cannot alter this (already fingerprinted) command (#78).
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported token counts captured server-side for a commit (§4).

    Actuals come from the provider's usage report, never caller-asserted.
    ``cached_input_tokens`` is the subset of ``input_tokens`` served from the
    provider's prompt cache (priced at the cached rate by the cost model).
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CommitCommand:
    """Reconcile a held reservation against actual usage (§4)."""

    idempotency_key: str
    reservation_id: ReservationId
    usage: ProviderUsage


@dataclass(frozen=True, slots=True)
class CancelCommand:
    """Release a held reservation whose call failed before incurring usage (§4)."""

    idempotency_key: str
    reservation_id: ReservationId


@dataclass(frozen=True, slots=True)
class ExtendCommand:
    """Heartbeat that advances a reservation's TTL while its call runs (§4, §5.4).

    Monotonic and naturally idempotent, so it carries no idempotency key.
    """

    reservation_id: ReservationId


@dataclass(frozen=True, slots=True)
class ReserveResult:
    """The outcome of a successful reserve (§4).

    A *denied* reserve raises a typed error (``InsufficientBudget`` naming the
    binding node, or ``BudgetNotFound`` on an empty applicable set) rather than
    returning a result. ``estimated_micro`` is the worst-case amount held on every
    applicable node; ``price_book_version`` pins the cost basis the matching commit
    reconciles against; ``ttl_deadline`` is when the reservation is reaped absent a
    heartbeat (§5.4).
    """

    reservation_id: ReservationId
    estimated_micro: int
    price_book_version: str
    ttl_deadline: datetime


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The reconciliation of a commit (§4).

    ``committed_micro + overage_micro`` is the actual cost. On the normal path every node
    receives the same split — at most the reserved estimate converts to committed spend and
    drift above it is audited overage — so the pair is each node's split. On the self-healing
    late commit of a reaped reservation (§5.4, ADR 0029) the split varies with each node's live
    remaining and the pair reports the most-restrictive node's split (the greatest overage);
    per-node detail is on the ledger.
    """

    reservation_id: ReservationId
    committed_micro: int
    overage_micro: int


@dataclass(frozen=True, slots=True)
class CancelResult:
    """The outcome of a cancel: the full estimate released on every line (§4)."""

    reservation_id: ReservationId
    released_micro: int


@dataclass(frozen=True, slots=True)
class ExtendResult:
    """The outcome of a heartbeat: the reservation's advanced TTL deadline (§5.4)."""

    reservation_id: ReservationId
    ttl_deadline: datetime


@dataclass(frozen=True, slots=True)
class GraceBackfillCommand:
    """Reconcile spend incurred during an enforcement outage under opt-in grace (§5.6).

    The SDK dispatched the call without a reservation while the datastore was unreachable and
    tracked the provider-reported usage locally; once connectivity returns it backfills that
    spend so it is never lost. There is no reservation to reconcile against — the applicable
    budget set, the price, and the period are resolved server-side at backfill time (ADR 0030).
    """

    idempotency_key: str
    provider: str
    model: str
    usage: ProviderUsage
    project_id: ProjectId | None = None


@dataclass(frozen=True, slots=True)
class GraceBackfillResult:
    """The outcome of a grace backfill: the recorded cost and its price basis (§5.6).

    Per-node committed/overage splits vary with each node's live remaining and are recorded on
    the ``grace_backfill`` ledger rows, not summarized here.
    """

    actual_micro: int
    price_book_version: str
