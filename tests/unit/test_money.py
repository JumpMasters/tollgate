"""Tests for micro-USD normalization."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tollgate.domain.money import MICRO_PER_USD, from_micro_usd, round_micro, to_micro_usd


def test_micro_per_usd_constant() -> None:
    assert MICRO_PER_USD == 1_000_000


@pytest.mark.parametrize(
    ("usd", "micro"),
    [
        (Decimal("0"), 0),
        (Decimal("1"), 1_000_000),
        (Decimal("1.50"), 1_500_000),
        (Decimal("0.000001"), 1),
        (Decimal("12.345678"), 12_345_678),
    ],
)
def test_to_micro_usd(usd: Decimal, micro: int) -> None:
    assert to_micro_usd(usd) == micro


def test_to_micro_usd_rounds_half_up() -> None:
    # 0.0000005 USD == 0.5 micro-USD, which rounds up to 1.
    assert to_micro_usd(Decimal("0.0000005")) == 1


def test_to_micro_usd_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        to_micro_usd(Decimal("-1"))


def test_from_micro_usd_inverts() -> None:
    assert from_micro_usd(1_500_000) == Decimal("1.500000")


def test_round_trip() -> None:
    assert to_micro_usd(from_micro_usd(2_500_000)) == 2_500_000


def test_from_micro_usd_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        from_micro_usd(-1)


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
