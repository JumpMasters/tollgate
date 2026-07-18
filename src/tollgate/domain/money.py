"""Monetary amounts in micro-USD.

Costs normalize to integer micro-USD (millionths of a US dollar) so that balance
arithmetic is exact and free of binary floating-point error. A ``Decimal`` amount
of US dollars converts to the nearest micro-USD using half-up rounding, the
conventional choice for money.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Final

from tollgate.domain.errors import AmountOutOfRange

#: Micro-USD per US dollar.
MICRO_PER_USD: Final = 1_000_000

_MICRO = Decimal(MICRO_PER_USD)

#: The largest micro-USD amount the signed ``BigInteger`` balance/ledger columns hold.
_MAX_MICRO: Final = Decimal(2**63 - 1)


def _checked(amount_micro: Decimal) -> Decimal:
    """Reject a micro-USD amount that is negative or beyond the representable int8 range.

    The range check runs before any quantize, so an oversized cost surfaces as a typed
    :class:`AmountOutOfRange` rather than a driver overflow at bind time or an untyped
    ``decimal.InvalidOperation`` past the default Decimal precision.
    """
    if amount_micro < 0:
        raise ValueError("monetary amounts must be non-negative")
    if amount_micro > _MAX_MICRO:
        raise AmountOutOfRange("monetary amount exceeds the representable range")
    return amount_micro


def round_micro(amount_micro: Decimal) -> int:
    """Round a micro-USD ``Decimal`` to the nearest whole micro-USD (half-up)."""
    return int(_checked(amount_micro).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ceil_micro(amount_micro: Decimal) -> int:
    """Round a micro-USD ``Decimal`` *up* to the next whole micro-USD.

    Used for worst-case reserve estimates, which must never under-reserve: ceiling
    rounding keeps a sub-micro worst case at ``>= 1`` micro rather than collapsing it
    to zero, so the held estimate always covers the eventual half-up-rounded actual.
    """
    return int(_checked(amount_micro).quantize(Decimal(1), rounding=ROUND_CEILING))


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
