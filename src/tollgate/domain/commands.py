"""Command and result value types for the four reservation commands (§4, §5).

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
