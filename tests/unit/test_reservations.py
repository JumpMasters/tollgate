"""Tests for the reservation state machine."""

from __future__ import annotations

import pytest

from tollgate.domain.reservations import ReservationStatus, can_transition, is_terminal

TERMINAL = [
    ReservationStatus.COMMITTED,
    ReservationStatus.RELEASED,
    ReservationStatus.REAPED,
]


def test_held_is_not_terminal() -> None:
    assert not is_terminal(ReservationStatus.HELD)


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_states_are_terminal(status: ReservationStatus) -> None:
    assert is_terminal(status)


@pytest.mark.parametrize("target", TERMINAL)
def test_held_can_reach_any_terminal(target: ReservationStatus) -> None:
    assert can_transition(ReservationStatus.HELD, target)


def test_held_cannot_stay_held() -> None:
    assert not can_transition(ReservationStatus.HELD, ReservationStatus.HELD)


@pytest.mark.parametrize("current", TERMINAL)
@pytest.mark.parametrize("target", [*TERMINAL, ReservationStatus.HELD])
def test_terminal_states_admit_no_transition(
    current: ReservationStatus, target: ReservationStatus
) -> None:
    assert not can_transition(current, target)


def test_status_values_are_stable_strings() -> None:
    assert ReservationStatus.HELD.value == "held"
    assert ReservationStatus.COMMITTED.value == "committed"
    assert ReservationStatus.RELEASED.value == "released"
    assert ReservationStatus.REAPED.value == "reaped"
