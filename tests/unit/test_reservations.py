"""Tests for the reservation state machine."""

from __future__ import annotations

import pytest

from tollgate.domain.reservations import ReservationStatus, can_transition, is_terminal

TERMINAL = [ReservationStatus.COMMITTED, ReservationStatus.RELEASED]
ALL_STATUSES = list(ReservationStatus)


def test_held_is_not_terminal() -> None:
    assert not is_terminal(ReservationStatus.HELD)


@pytest.mark.parametrize("status", TERMINAL)
def test_committed_and_released_are_terminal(status: ReservationStatus) -> None:
    assert is_terminal(status)


def test_reaped_is_not_terminal_a_late_commit_may_follow() -> None:
    # §5.4: a commit for a reaped reservation self-heals into committed (ADR 0029).
    assert not is_terminal(ReservationStatus.REAPED)


@pytest.mark.parametrize("target", [*TERMINAL, ReservationStatus.REAPED])
def test_held_can_settle_each_way(target: ReservationStatus) -> None:
    assert can_transition(ReservationStatus.HELD, target)


def test_held_cannot_stay_held() -> None:
    assert not can_transition(ReservationStatus.HELD, ReservationStatus.HELD)


def test_reaped_admits_only_the_late_commit() -> None:
    assert can_transition(ReservationStatus.REAPED, ReservationStatus.COMMITTED)
    assert not can_transition(ReservationStatus.REAPED, ReservationStatus.RELEASED)
    assert not can_transition(ReservationStatus.REAPED, ReservationStatus.HELD)
    assert not can_transition(ReservationStatus.REAPED, ReservationStatus.REAPED)


@pytest.mark.parametrize("current", TERMINAL)
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_terminal_states_admit_no_transition(
    current: ReservationStatus, target: ReservationStatus
) -> None:
    assert not can_transition(current, target)


def test_status_values_are_stable_strings() -> None:
    assert ReservationStatus.HELD.value == "held"
    assert ReservationStatus.COMMITTED.value == "committed"
    assert ReservationStatus.RELEASED.value == "released"
    assert ReservationStatus.REAPED.value == "reaped"
