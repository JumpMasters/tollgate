"""SystemClock: the real wall-clock source injected into handlers (the Clock port)."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Returns the current instant as a timezone-aware UTC ``datetime``."""

    def now(self) -> datetime:
        """Return ``datetime.now(UTC)``."""
        return datetime.now(UTC)
