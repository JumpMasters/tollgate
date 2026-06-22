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
