"""Tests for the provider-qualified cost model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tollgate.domain.pricing import ModelPrice, actual_micro, estimate_micro

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


def test_actual_prices_all_input_at_full_rate_by_default() -> None:
    # 2.5 * 1000 + 10 * 200 = 2500 + 2000
    assert actual_micro(PRICE, input_tokens=1000, output_tokens=200) == 4500


def test_actual_prices_cached_subset_at_cached_rate() -> None:
    # non-cached 600 @ 2.5 = 1500; cached 400 @ 1.25 = 500; output 200 @ 10 = 2000
    assert (
        actual_micro(PRICE, input_tokens=1000, output_tokens=200, cached_input_tokens=400) == 4000
    )


def test_actual_rejects_cached_exceeding_input() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        actual_micro(PRICE, input_tokens=100, output_tokens=0, cached_input_tokens=101)


def test_actual_rejects_negative_tokens() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        actual_micro(PRICE, input_tokens=-1, output_tokens=0)
    with pytest.raises(ValueError, match="non-negative"):
        actual_micro(PRICE, input_tokens=0, output_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        actual_micro(PRICE, input_tokens=0, output_tokens=0, cached_input_tokens=-1)
