"""Budget-tree scopes: applicable-set resolution and the deterministic lock order.

A budget governs one *node* of the tenancy tree — an ``org``, a ``team``, a
``user``, or an orthogonal ``project``. A reserve gates against the **applicable
set**: the principal's ancestry path (those of org/team/user that actually carry
a budget) together with the request's project budget when the credential
authorizes it. The set is locked and updated in one canonical order so that
overlapping reserves by sibling users cannot deadlock on the shared parent rows,
and an *empty* set is denied by default — a request governed by no budget
is not, by the thesis, safely admissible.

This module is pure policy over already-resolved nodes: *which* budgets exist for
a principal is I/O the application performs against the repository before calling
in here. No I/O, no internal imports beyond sibling ``domain`` modules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tollgate.domain.errors import BudgetNotFound, ConflictingBudgetScope
from tollgate.domain.ids import BudgetId, OrgId


class ScopeKind(StrEnum):
    """The kind of tenancy node a budget governs."""

    ORG = "org"
    TEAM = "team"
    USER = "user"
    PROJECT = "project"


#: Lock-ordering rank: org < team < user < project. Every command and the
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


@dataclass(frozen=True, slots=True)
class ReserveOutcome:
    """The result of a multi-budget reserve across an applicable set.

    ``ok`` is true iff every node had headroom and was reserved. On denial ``ok`` is
    false and ``binding_node`` names the most-restrictive node that lacked headroom
    (most-restrictive resolution); the all-or-nothing rollback that discards the
    partial reserves on the earlier nodes is the command envelope's, not
    this value's.
    """

    ok: bool
    binding_node: BudgetNode | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    """A request's named project resolved server-side for authorization and the applicable set.

    ``org_id`` is the project's owning org, looked up server-side so the reserve can build the
    authorization ancestry ``{ORG: org_id, PROJECT: project_id}`` from trusted data — never the
    request's assertion (the trust caveat in ``authorizes``). ``budget`` is the project's
    budget node when it carries one, or ``None``: a project may be authorized yet contribute no
    budget to the applicable set.
    """

    org_id: OrgId
    budget: BudgetNode | None


@dataclass(frozen=True, slots=True)
class ScopeRef:
    """A reference to a scope node (kind + id) independent of any budget.

    The chargeback read API uses it to name the optional filter node whose subtree a
    caller wants, before any budget is resolved. Unlike ``BudgetNode`` it carries no ``budget_id``.
    """

    scope_kind: ScopeKind
    scope_id: str


def lock_order_key(node: BudgetNode) -> tuple[int, str]:
    """Canonical sort key for deadlock-free lock acquisition.

    Orders by ``(scope_kind rank, scope_id)``. The storage-layer order is the
    3-tuple ``(scope_kind rank, scope_id, period_start)``; within a single
    reserve every applicable balance shares one ``period_start``, so this
    node-level key is the operative order, and ``period_start`` enters as the
    final ``ORDER BY`` column where balance rows are loaded.
    """
    return (scope_rank(node.scope_kind), node.scope_id)


def resolve_applicable_set(
    ancestry: Iterable[BudgetNode],
    project: BudgetNode | None = None,
) -> tuple[BudgetNode, ...]:
    """Assemble the applicable budget set for a reserve, canonically ordered.

    ``ancestry`` is the org/team/user budget nodes that **exist** for the
    principal — the caller has already skipped ancestry scopes without a budget
    (that lookup is I/O, performed before calling here). ``project`` is the
    request's project budget, supplied only when the request named a project
    **and** the credential authorizes it **and** it carries a budget; ``None``
    otherwise.

    Budgets are de-duplicated by ``budget_id`` — the row the persistence layer
    locks and updates per line, so a budget is charged exactly once — and returned
    sorted by :func:`lock_order_key`. V1 carries at most one budget per
    ``(scope_kind, scope_id)`` node (ADR 0025); two *distinct* budgets on one node
    is a configuration the schema forbids, so it is rejected with
    :class:`ConflictingBudgetScope` rather than silently dropping one (which could
    admit a reserve a budget should deny). Raises :class:`BudgetNotFound` if the
    resulting set is empty: a request governed by no budget is denied by default,
    never vacuously admitted.
    """
    by_budget: dict[BudgetId, BudgetNode] = {}
    owner: dict[tuple[ScopeKind, str], BudgetId] = {}
    candidates = list(ancestry)
    if project is not None:
        candidates.append(project)
    for node in candidates:
        scope = (node.scope_kind, node.scope_id)
        held = owner.get(scope)
        if held is not None and held != node.budget_id:
            raise ConflictingBudgetScope(node.scope_kind, node.scope_id)
        owner.setdefault(scope, node.budget_id)
        by_budget.setdefault(node.budget_id, node)
    if not by_budget:
        raise BudgetNotFound("no budget governs the request")
    return tuple(sorted(by_budget.values(), key=lock_order_key))
