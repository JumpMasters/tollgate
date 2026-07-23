"""The reservation state machine.

A reservation is *held* when created. *Committed* and *released* are terminal; *reaped* (its
TTL elapsed with no heartbeat) is settled but not dead: the one transition out of it is the
self-healing late commit — ``reaped → committed`` — which records real spend for a call
that was still alive when its reservation was reaped (ADR 0029). The transitions here are
pure; persistence and the identity guards that make each transition exactly-once live in the
adapters.
"""

from __future__ import annotations

from enum import StrEnum


class ReservationStatus(StrEnum):
    """Lifecycle states of a reservation."""

    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"
    REAPED = "reaped"


#: The legal transitions: held settles any way; reaped admits only the late commit.
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
    ReservationStatus.REAPED: frozenset({ReservationStatus.COMMITTED}),
}


def is_terminal(status: ReservationStatus) -> bool:
    """Return whether ``status`` admits no further transition."""
    return not _ALLOWED[status]


def can_transition(current: ReservationStatus, target: ReservationStatus) -> bool:
    """Return whether moving from ``current`` to ``target`` is a legal transition."""
    return target in _ALLOWED[current]
