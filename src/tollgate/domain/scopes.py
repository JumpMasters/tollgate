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

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tollgate.domain.errors import BudgetNotFound
from tollgate.domain.ids import BudgetId


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


@dataclass(frozen=True, slots=True)
class BudgetNode:
    """One budget on the enforcement path, identified by the node it governs.

    ``scope_id`` is the id of the governed node (an org/team/user/project id),
    carried as ``str`` because the kind is orthogonal to the id's type.
    ``budget_id`` is the budget row the persistence layer locks and updates.
    """

    budget_id: BudgetId
    scope_kind: ScopeKind
    scope_id: str


def lock_order_key(node: BudgetNode) -> tuple[int, str]:
    """Canonical sort key for deadlock-free lock acquisition (§5.3).

    Orders by ``(scope_kind rank, scope_id)``. The storage-layer order is the
    3-tuple ``(scope_kind rank, scope_id, period_start)``; within a single
    reserve every applicable balance shares one ``period_start``, so this
    node-level key is the operative order, and ``period_start`` enters as the
    final ``ORDER BY`` column where balance rows are loaded (plans 05/07).
    """
    return (scope_rank(node.scope_kind), node.scope_id)


def resolve_applicable_set(
    ancestry: Iterable[BudgetNode],
    project: BudgetNode | None = None,
) -> tuple[BudgetNode, ...]:
    """Assemble the applicable budget set for a reserve, canonically ordered (§4, §5.3).

    ``ancestry`` is the org/team/user budget nodes that **exist** for the
    principal — the caller has already skipped ancestry scopes without a budget
    (that lookup is I/O, performed before calling here). ``project`` is the
    request's project budget, supplied only when the request named a project
    **and** the credential authorizes it **and** it carries a budget; ``None``
    otherwise.

    Nodes are de-duplicated by ``(scope_kind, scope_id)`` — a node must be locked
    and charged exactly once — and returned sorted by :func:`lock_order_key`.
    Raises :class:`BudgetNotFound` if the resulting set is empty: a request
    governed by no budget is denied by default (§5.3), never vacuously admitted.
    """
    nodes: dict[tuple[ScopeKind, str], BudgetNode] = {}
    for node in ancestry:
        nodes.setdefault((node.scope_kind, node.scope_id), node)
    if project is not None:
        nodes.setdefault((project.scope_kind, project.scope_id), project)
    if not nodes:
        raise BudgetNotFound("no budget governs the request")
    return tuple(sorted(nodes.values(), key=lock_order_key))
