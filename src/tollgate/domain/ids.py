"""Strongly-typed identifiers for the domain entities.

Each identifier is a distinct ``NewType`` over ``str`` so that, for example, a
``TeamId`` cannot be passed where a ``UserId`` is expected. They carry no
behaviour; construction and generation live in the adapters.
"""

from __future__ import annotations

from typing import NewType

OrgId = NewType("OrgId", str)
TeamId = NewType("TeamId", str)
UserId = NewType("UserId", str)
ProjectId = NewType("ProjectId", str)
BudgetId = NewType("BudgetId", str)
ReservationId = NewType("ReservationId", str)
PrincipalId = NewType("PrincipalId", str)
LedgerEntryId = NewType("LedgerEntryId", str)
