"""Tests for the provider-qualified cost model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tollgate.domain.pricing import ModelPrice, estimate_micro

# $2.50 / 1M input, $10.00 / 1M output, $1.25 / 1M cached input, as micro-USD per token.
PRICE = ModelPrice(
    provider="openai",
    model="gpt-4o",
    input_micro_per_token=Decimal("2.5"),
    output_micro_per_token=Decimal("10"),
    cached_input_micro_per_token=Decimal("1.25"),
)


def test_model_price_is_immutable() -> None:
    with pytest.raises(AttributeError):
        PRICE.input_micro_per_token = Decimal("9")  # type: ignore[misc]


def test_estimate_is_input_bound_plus_output_ceiling() -> None:
    # 2.5 * 1000 + 10 * 500 = 2500 + 5000
    assert estimate_micro(PRICE, input_bound_tokens=1000, max_output_tokens=500) == 7500


def test_estimate_rounds_half_up() -> None:
    cheap = ModelPrice(
        provider="openai",
        model="tiny",
        input_micro_per_token=Decimal("0.5"),
        output_micro_per_token=Decimal("0"),
        cached_input_micro_per_token=Decimal("0"),
    )
    # 0.5 * 1 + 0 → 0.5 → rounds half-up to 1
    assert estimate_micro(cheap, input_bound_tokens=1, max_output_tokens=0) == 1


def test_estimate_rejects_negative_tokens() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        estimate_micro(PRICE, input_bound_tokens=-1, max_output_tokens=10)
    with pytest.raises(ValueError, match="non-negative"):
        estimate_micro(PRICE, input_bound_tokens=10, max_output_tokens=-1)
