"""Monetary amounts in micro-USD.

Costs normalize to integer micro-USD (millionths of a US dollar) so that balance
arithmetic is exact and free of binary floating-point error. A ``Decimal`` amount
of US dollars converts to the nearest micro-USD using half-up rounding, the
conventional choice for money.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

#: Micro-USD per US dollar.
MICRO_PER_USD: Final = 1_000_000

_MICRO = Decimal(MICRO_PER_USD)


def round_micro(amount_micro: Decimal) -> int:
    """Round a micro-USD ``Decimal`` to the nearest whole micro-USD (half-up)."""
    if amount_micro < 0:
        raise ValueError("monetary amounts must be non-negative")
    return int(amount_micro.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_micro_usd(usd: Decimal) -> int:
    """Convert an amount of US dollars to integer micro-USD (rounded half-up)."""
    if usd < 0:
        raise ValueError("monetary amounts must be non-negative")
    return round_micro(usd * _MICRO)


def from_micro_usd(micro: int) -> Decimal:
    """Convert integer micro-USD back to a US-dollar ``Decimal``."""
    if micro < 0:
        raise ValueError("monetary amounts must be non-negative")
    return (Decimal(micro) / _MICRO).quantize(Decimal("0.000001"))
