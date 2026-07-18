"""Tests for the provider-qualified cost model."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tollgate.domain.errors import AmountOutOfRange
from tollgate.domain.pricing import (
    ModelPrice,
    PricedModel,
    Reconciliation,
    actual_micro,
    estimate_micro,
    reconcile,
)

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


def test_model_price_rejects_cached_rate_above_input_rate() -> None:
    # A cache discount can never cost more than the full input rate; a price book
    # seeded that way would manufacture spurious overage on an all-cached call.
    with pytest.raises(ValueError, match="cached input rate"):
        ModelPrice(
            provider="openai",
            model="misconfigured",
            input_micro_per_token=Decimal("2.5"),
            output_micro_per_token=Decimal("10"),
            cached_input_micro_per_token=Decimal("3.0"),
        )


def test_model_price_rejects_negative_rate() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ModelPrice(
            provider="openai",
            model="misconfigured",
            input_micro_per_token=Decimal("-1"),
            output_micro_per_token=Decimal("10"),
            cached_input_micro_per_token=Decimal("0"),
        )


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


def test_estimate_out_of_range_raises_typed_error() -> None:
    # A worst-case estimate that overflows the int8 balance columns surfaces as a
    # typed AmountOutOfRange, not a bare Decimal/driver overflow (#66).
    with pytest.raises(AmountOutOfRange):
        estimate_micro(PRICE, input_bound_tokens=2**63, max_output_tokens=0)


def test_actual_prices_all_input_at_full_rate_by_default() -> None:
    # 2.5 * 1000 + 10 * 200 = 2500 + 2000
    assert actual_micro(PRICE, input_tokens=1000, output_tokens=200) == 4500


def test_actual_prices_cached_subset_at_cached_rate() -> None:
    # non-cached 600 @ 2.5 = 1500; cached 400 @ 1.25 = 500; output 200 @ 10 = 2000
    assert (
        actual_micro(PRICE, input_tokens=1000, output_tokens=200, cached_input_tokens=400) == 4000
    )


def test_actual_prices_full_cache_at_cached_rate() -> None:
    # Boundary cached == input: non-cached 0, all 1000 input @ 1.25 = 1250.
    assert actual_micro(PRICE, input_tokens=1000, output_tokens=0, cached_input_tokens=1000) == 1250


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


def test_actual_out_of_range_raises_typed_error() -> None:
    # The same guard covers the commit/grace path through actual_micro (#66).
    with pytest.raises(AmountOutOfRange):
        actual_micro(PRICE, input_tokens=2**63, output_tokens=0)


def test_reconcile_under_reservation_has_no_overage() -> None:
    result = reconcile(reserved_micro=7500, actual=4000)
    assert result == Reconciliation(committed_micro=4000, overage_micro=0)


def test_reconcile_over_reservation_records_overage() -> None:
    result = reconcile(reserved_micro=4000, actual=7500)
    assert result == Reconciliation(committed_micro=4000, overage_micro=3500)


def test_reconcile_exact_reservation() -> None:
    result = reconcile(reserved_micro=4000, actual=4000)
    assert result == Reconciliation(committed_micro=4000, overage_micro=0)


def test_reconcile_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        reconcile(reserved_micro=-1, actual=0)
    with pytest.raises(ValueError, match="non-negative"):
        reconcile(reserved_micro=0, actual=-1)


@given(
    reserved=st.integers(min_value=0, max_value=10**15),
    actual=st.integers(min_value=0, max_value=10**15),
)
def test_reconcile_conserves_actual_and_caps_committed(reserved: int, actual: int) -> None:
    result = reconcile(reserved_micro=reserved, actual=actual)
    # The whole actual is accounted for, split between committed and overage.
    assert result.committed_micro + result.overage_micro == actual
    # Commit never moves more than the reservation into committed.
    assert result.committed_micro <= reserved
    assert result.committed_micro <= actual
    assert result.overage_micro >= 0


def test_priced_model_carries_its_version_and_price() -> None:
    price = ModelPrice(
        provider="anthropic",
        model="claude",
        input_micro_per_token=Decimal("1"),
        output_micro_per_token=Decimal("2"),
        cached_input_micro_per_token=Decimal("0.5"),
    )
    priced = PricedModel(version="2026-06-22", price=price)
    assert priced.version == "2026-06-22"
    assert priced.price is price
