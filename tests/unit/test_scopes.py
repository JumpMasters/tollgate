"""Tests for budget-tree scope kinds, ordering, and applicable-set resolution."""

from __future__ import annotations

import pytest

from tollgate.domain.ids import BudgetId
from tollgate.domain.scopes import BudgetNode, ScopeKind, lock_order_key, scope_rank


def test_scope_kind_values() -> None:
    assert ScopeKind.ORG == "org"  # type: ignore[comparison-overlap]
    assert ScopeKind.TEAM == "team"  # type: ignore[comparison-overlap]
    assert ScopeKind.USER == "user"  # type: ignore[comparison-overlap]
    assert ScopeKind.PROJECT == "project"  # type: ignore[comparison-overlap]


def test_scope_rank_orders_org_team_user_project() -> None:
    assert (
        scope_rank(ScopeKind.ORG)
        < scope_rank(ScopeKind.TEAM)
        < scope_rank(ScopeKind.USER)
        < scope_rank(ScopeKind.PROJECT)
    )


def test_scope_rank_defined_for_every_kind() -> None:
    ranks = [scope_rank(kind) for kind in ScopeKind]
    assert sorted(ranks) == [0, 1, 2, 3]


def _node(scope_kind: ScopeKind, scope_id: str, budget_id: str = "b") -> BudgetNode:
    return BudgetNode(budget_id=BudgetId(budget_id), scope_kind=scope_kind, scope_id=scope_id)


def test_budget_node_is_immutable() -> None:
    node = _node(ScopeKind.ORG, "o1")
    with pytest.raises(AttributeError):
        node.scope_id = "o2"  # type: ignore[misc]


def test_lock_order_key_ranks_by_scope_kind() -> None:
    org = _node(ScopeKind.ORG, "o1")
    team = _node(ScopeKind.TEAM, "t1")
    user = _node(ScopeKind.USER, "u1")
    project = _node(ScopeKind.PROJECT, "p1")
    shuffled = [project, user, org, team]
    assert sorted(shuffled, key=lock_order_key) == [org, team, user, project]


def test_lock_order_key_breaks_ties_by_scope_id() -> None:
    # Same kind → ordered by scope_id (the storage-layer order needs this tiebreak
    # across the global set, e.g. the reaper touching many nodes).
    a = _node(ScopeKind.USER, "u-aaa")
    b = _node(ScopeKind.USER, "u-bbb")
    assert sorted([b, a], key=lock_order_key) == [a, b]
