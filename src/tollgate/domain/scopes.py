"""Budget-tree scopes: applicable-set resolution and the deterministic lock order.

A budget governs one *node* of the tenancy tree — an ``org``, a ``team``, a
``user``, or an orthogonal ``project``. A reserve gates against the **applicable
set**: the principal's ancestry path (those of org/team/user that actually carry
a budget) together with the request's project budget when the credential
authorizes it. The set is locked and updated in one canonical order so that
overlapping reserves by sibling users cannot deadlock on the shared parent rows
(§5.3), and an *empty* set is denied by default — a request governed by no budget
is not, by the thesis, safely admissible (§5.3).

This module is pure policy over already-resolved nodes: *which* budgets exist for
a principal is I/O the application performs against the repository before calling
in here. No I/O, no internal imports beyond sibling ``domain`` modules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ScopeKind(StrEnum):
    """The kind of tenancy node a budget governs."""

    ORG = "org"
    TEAM = "team"
    USER = "user"
    PROJECT = "project"


#: Lock-ordering rank: org < team < user < project (§5.3). Every command and the
#: reaper acquire budget rows in this order, so overlapping operations on shared
#: parent rows cannot form a lock cycle.
_SCOPE_RANK: Final[dict[ScopeKind, int]] = {
    ScopeKind.ORG: 0,
    ScopeKind.TEAM: 1,
    ScopeKind.USER: 2,
    ScopeKind.PROJECT: 3,
}


def scope_rank(kind: ScopeKind) -> int:
    """Return the lock-ordering rank of ``kind`` (org < team < user < project)."""
    return _SCOPE_RANK[kind]
