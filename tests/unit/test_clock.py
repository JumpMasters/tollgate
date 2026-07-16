"""Tests for the SystemClock adapter."""

from __future__ import annotations

from datetime import UTC, datetime

from tollgate.adapters.clock import SystemClock


def test_system_clock_returns_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now(UTC).utcoffset()
