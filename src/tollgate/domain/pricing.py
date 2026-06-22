"""The provider-qualified cost model.

Prices live in a versioned, immutable price book (§3); this module is the pure
arithmetic over one *resolved* price: the worst-case reserve estimate, the
reconciled actual cost from provider-reported usage, and the split of an actual
against its reservation into committed-versus-overage. Amounts are integer
micro-USD; per-token rates are ``Decimal`` so sub-micro rates stay exact until the
final half-up rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tollgate.domain.money import round_micro


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Micro-USD-per-token rates for one (provider, model) at one price-book version.

    Rates are ``Decimal`` micro-USD per single token: an input price of ``$2.50``
    per million tokens is ``Decimal("2.5")`` micro-USD per token.
    """

    provider: str
    model: str
    input_micro_per_token: Decimal
    output_micro_per_token: Decimal
    cached_input_micro_per_token: Decimal

    def __post_init__(self) -> None:
        if (
            self.input_micro_per_token < 0
            or self.output_micro_per_token < 0
            or self.cached_input_micro_per_token < 0
        ):
            raise ValueError("per-token rates must be non-negative")
        if self.cached_input_micro_per_token > self.input_micro_per_token:
            raise ValueError("cached input rate cannot exceed the full input rate")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The split of an actual cost against its reservation (§4).

    ``committed_micro`` is the part of the reservation that converts to real spend
    (at most the reserved estimate); ``overage_micro`` is audited drift above it.
    """

    committed_micro: int
    overage_micro: int


def estimate_micro(price: ModelPrice, *, input_bound_tokens: int, max_output_tokens: int) -> int:
    """Worst-case reserve estimate: full (non-cached) input price + output ceiling.

    ``input_bound_tokens`` is the tokenizer-derived upper bound on the prompt and
    ``max_output_tokens`` the provider ceiling, so this over-reserves in the safe
    direction (§4). Returns integer micro-USD.
    """
    if input_bound_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    total = (
        price.input_micro_per_token * input_bound_tokens
        + price.output_micro_per_token * max_output_tokens
    )
    return round_micro(total)


def actual_micro(
    price: ModelPrice,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> int:
    """Reconciled cost from provider-reported usage (§4).

    ``cached_input_tokens`` is the subset of ``input_tokens`` served from the
    provider's prompt cache, priced at the cached rate; the remaining input tokens
    and all output tokens price at the full rates. Returns integer micro-USD.
    """
    if input_tokens < 0 or output_tokens < 0 or cached_input_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached input tokens cannot exceed input tokens")
    non_cached = input_tokens - cached_input_tokens
    total = (
        price.input_micro_per_token * non_cached
        + price.cached_input_micro_per_token * cached_input_tokens
        + price.output_micro_per_token * output_tokens
    )
    return round_micro(total)


def reconcile(*, reserved_micro: int, actual: int) -> Reconciliation:
    """Split an actual cost against its reservation into committed and overage (§4).

    Mirrors the SQL ``LEAST(:actual, :est)`` / ``GREATEST(:actual - :est, 0)`` in the
    commit guard (§5.2): commit moves at most the reserved estimate; any excess is
    audited overage. ``actual`` is the reconciled cost from :func:`actual_micro`.
    """
    if reserved_micro < 0 or actual < 0:
        raise ValueError("monetary amounts must be non-negative")
    committed = min(actual, reserved_micro)
    overage = max(actual - reserved_micro, 0)
    return Reconciliation(committed_micro=committed, overage_micro=overage)
