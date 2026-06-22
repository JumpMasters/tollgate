"""The reservation state machine.

A reservation is *held* when created, then reaches exactly one terminal state:
*committed* (the call completed and usage was reconciled), *released* (the call
was cancelled before incurring usage), or *reaped* (its TTL elapsed with no
heartbeat). The transitions here are pure; persistence and the identity guard
that makes a terminal effect exactly-once live in the adapters.
"""

from __future__ import annotations

from enum import StrEnum


class ReservationStatus(StrEnum):
    """Lifecycle states of a reservation."""

    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"
    REAPED = "reaped"


#: The only legal transitions: a held reservation may reach any terminal state.
_ALLOWED: dict[ReservationStatus, frozenset[ReservationStatus]] = {
    ReservationStatus.HELD: frozenset(
        {
            ReservationStatus.COMMITTED,
            ReservationStatus.RELEASED,
            ReservationStatus.REAPED,
        }
    ),
    ReservationStatus.COMMITTED: frozenset(),
    ReservationStatus.RELEASED: frozenset(),
    ReservationStatus.REAPED: frozenset(),
}


def is_terminal(status: ReservationStatus) -> bool:
    """Return whether ``status`` admits no further transition."""
    return not _ALLOWED[status]


def can_transition(current: ReservationStatus, target: ReservationStatus) -> bool:
    """Return whether moving from ``current`` to ``target`` is a legal transition."""
    return target in _ALLOWED[current]
