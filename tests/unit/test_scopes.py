"""Tests for budget-tree scope kinds, ordering, and applicable-set resolution."""

from __future__ import annotations

from tollgate.domain.scopes import ScopeKind, scope_rank


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
