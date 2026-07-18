"""Monetary amounts in micro-USD.

Costs normalize to integer micro-USD (millionths of a US dollar) so that balance
arithmetic is exact and free of binary floating-point error. A ``Decimal`` amount
of US dollars converts to the nearest micro-USD using half-up rounding, the
conventional choice for money.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from tollgate.domain.errors import AmountOutOfRange

#: Micro-USD per US dollar.
MICRO_PER_USD: Final = 1_000_000

_MICRO = Decimal(MICRO_PER_USD)

#: The largest micro-USD amount the signed ``BigInteger`` balance/ledger columns hold.
_MAX_MICRO: Final = Decimal(2**63 - 1)


def round_micro(amount_micro: Decimal) -> int:
    """Round a micro-USD ``Decimal`` to the nearest whole micro-USD (half-up).

    Amounts above the int8 ceiling raise :class:`AmountOutOfRange` before quantizing,
    so an oversized cost surfaces as a typed error instead of a driver overflow or an
    untyped ``decimal.InvalidOperation`` past the default Decimal precision.
    """
    if amount_micro < 0:
        raise ValueError("monetary amounts must be non-negative")
    if amount_micro > _MAX_MICRO:
        raise AmountOutOfRange("monetary amount exceeds the representable range")
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
