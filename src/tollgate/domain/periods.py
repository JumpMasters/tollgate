"""Period-start derivation for the reserve path (§3, §5.5, ADR 0027).

A ``budget_balance`` is keyed by ``(budget_id, period_start)``, and the multi-budget reserve
(plan 07) applies a single ``period_start`` to every applicable node. V1 enforces one
**calendar-month** period shared by all of them: the schema also defines ``rolling_days``, but
that kind has no anchor column to derive a window start from, so selecting it on the reserve path
is deferred (ADR 0027). This module is pure — no I/O, no internal imports beyond the standard
library.
"""

from __future__ import annotations

from datetime import UTC, datetime


def calendar_month_start(now: datetime) -> datetime:
    """Return the first instant of ``now``'s UTC calendar month (ADR 0027).

    ``now`` must be timezone-aware; it is converted to UTC before the month is taken, so a
    timestamp near a month boundary in another zone rolls to the correct UTC period. The result is
    ``YYYY-MM-01T00:00:00+00:00`` — the ``period_start`` every applicable budget shares for the
    reserve.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    in_utc = now.astimezone(UTC)
    return datetime(in_utc.year, in_utc.month, 1, tzinfo=UTC)
