"""Tests for micro-USD normalization."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tollgate.domain.errors import AmountOutOfRange
from tollgate.domain.money import (
    MICRO_PER_USD,
    ceil_micro,
    round_micro,
)


def test_micro_per_usd_constant() -> None:
    assert MICRO_PER_USD == 1_000_000


def test_round_micro_rounds_half_up() -> None:
    assert round_micro(Decimal("0.5")) == 1
    assert round_micro(Decimal("2.5")) == 3
    assert round_micro(Decimal("2.4")) == 2


def test_round_micro_passes_whole_amounts_through() -> None:
    assert round_micro(Decimal("100")) == 100
    assert round_micro(Decimal("0")) == 0


def test_round_micro_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        round_micro(Decimal("-1"))


def test_round_micro_admits_the_int8_ceiling() -> None:
    # The largest amount the BigInteger balance/ledger columns can hold passes.
    assert round_micro(Decimal(2**63 - 1)) == 2**63 - 1


def test_round_micro_rejects_amount_above_int8_max() -> None:
    # A micro amount that overflows the BigInteger columns must surface as a typed
    # domain error, never a bare asyncpg out-of-range error at bind time (#66).
    with pytest.raises(AmountOutOfRange):
        round_micro(Decimal(2**63))


def test_round_micro_rejects_amounts_beyond_decimal_precision() -> None:
    # 1e28 exceeds the default Decimal precision; the guard fires before quantize,
    # so this is AmountOutOfRange rather than an untyped decimal.InvalidOperation (#66).
    with pytest.raises(AmountOutOfRange):
        round_micro(Decimal(10) ** 28)


def test_ceil_micro_rounds_up_any_fraction() -> None:
    # Ceiling rounding never rounds down, so a sub-micro worst case still reserves >= 1 (#77).
    assert ceil_micro(Decimal("0.4")) == 1
    assert ceil_micro(Decimal("0.5")) == 1
    assert ceil_micro(Decimal("1.000001")) == 2


def test_ceil_micro_passes_whole_amounts_through() -> None:
    assert ceil_micro(Decimal("100")) == 100
    assert ceil_micro(Decimal("0")) == 0


def test_ceil_micro_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ceil_micro(Decimal("-1"))


def test_ceil_micro_rejects_amount_above_int8_max() -> None:
    # The int8 ceiling guard applies to the ceiling path too (#66/#77).
    with pytest.raises(AmountOutOfRange):
        ceil_micro(Decimal(2**63))
