"""Tests for calendar-month period-start derivation (ADR 0027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tollgate.domain.periods import calendar_month_start


def test_mid_month_rolls_back_to_the_first() -> None:
    start = calendar_month_start(datetime(2026, 6, 23, 12, 30, 5, tzinfo=UTC))
    assert start == datetime(2026, 6, 1, tzinfo=UTC)


def test_first_instant_of_a_month_is_its_own_period_start() -> None:
    assert calendar_month_start(datetime(2026, 1, 1, tzinfo=UTC)) == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_december_stays_in_december() -> None:
    assert calendar_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2026, 12, 1, tzinfo=UTC
    )


def test_non_utc_input_is_converted_before_the_month_is_taken() -> None:
    # 2026-07-01 01:00 at +13:00 is 2026-06-30 12:00 UTC -> the June period, not July.
    east = timezone(timedelta(hours=13))
    assert calendar_month_start(datetime(2026, 7, 1, 1, 0, tzinfo=east)) == datetime(
        2026, 6, 1, tzinfo=UTC
    )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar_month_start(datetime(2026, 6, 23, 12, 0))
