"""Unit tests for ChargebackHandler: the at-or-below-scope authorization filter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.read import ChargebackHandler
from tollgate.domain.chargeback import BudgetState, GroupBy, GroupByKind, SpendGroup
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import ScopeNotAuthorized
from tollgate.domain.ids import (
    BudgetId,
    CredentialId,
    OrgId,
    PrincipalId,
    TeamId,
    UserId,
)
from tollgate.domain.invariants import Balance
from tollgate.domain.scopes import ScopeKind, ScopeRef

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
_PERIOD = datetime(2026, 7, 1, tzinfo=UTC)


class _FakeRepo:
    def __init__(
        self,
        *,
        ancestry: Mapping[tuple[ScopeKind, str], Mapping[ScopeKind, str]],
        states: Sequence[BudgetState],
        groups: Sequence[SpendGroup] = (),
    ) -> None:
        self._ancestry = ancestry
        self._states = states
        self._groups = groups
        self.subtree_calls: list[tuple[ScopeKind, str, datetime]] = []
        self.spend_calls: list[tuple[ScopeKind, str, datetime, GroupBy]] = []

    async def subtree_states(
        self, scope_kind: ScopeKind, scope_id: str, period_start: datetime
    ) -> Sequence[BudgetState]:
        self.subtree_calls.append((scope_kind, scope_id, period_start))
        return self._states

    async def resolve_scope_ancestry(
        self, scope_kind: ScopeKind, scope_id: str
    ) -> Mapping[ScopeKind, str] | None:
        return self._ancestry.get((scope_kind, scope_id))

    async def spend_rollup(
        self,
        scope_kind: ScopeKind,
        scope_id: str,
        period_start: datetime,
        group_by: GroupBy,
    ) -> Sequence[SpendGroup]:
        self.spend_calls.append((scope_kind, scope_id, period_start, group_by))
        return self._groups


class _FakeReader:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_FakeRepo]:
        yield self._repo


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


def _credential(scope_kind: ScopeKind, scope_id: str) -> Credential:
    return Credential(
        credential_id=CredentialId("c1"),
        principal_id=PrincipalId("u1"),
        scope_kind=scope_kind,
        scope_id=scope_id,
        status=CredentialStatus.ACTIVE,
    )


def _auth(scope_kind: ScopeKind, scope_id: str) -> AuthContext:
    return AuthContext(
        credential=_credential(scope_kind, scope_id),
        principal=Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1")),
    )


def _one_state() -> BudgetState:
    return BudgetState(
        budget_id=BudgetId("b-user"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        balance=Balance(limit_micro=1000, reserved_micro=0, committed_micro=0, overage_micro=0),
        alert_thresholds_pct=(),
    )


def _handler(repo: _FakeRepo) -> ChargebackHandler:
    return ChargebackHandler(reader=_FakeReader(repo), clock=_FixedClock())


async def test_no_filter_reads_the_credentials_own_subtree_for_the_current_period() -> None:
    repo = _FakeRepo(ancestry={}, states=[_one_state()])
    view = await _handler(repo).budget_states(_auth(ScopeKind.USER, "u1"))
    assert repo.subtree_calls == [(ScopeKind.USER, "u1", _PERIOD)]
    assert view.period_start == _PERIOD
    assert view.states == (_one_state(),)


async def test_authorized_filter_rewrites_the_subtree_root() -> None:
    repo = _FakeRepo(
        ancestry={(ScopeKind.TEAM, "t1"): {ScopeKind.ORG: "o1", ScopeKind.TEAM: "t1"}},
        states=[_one_state()],
    )
    await _handler(repo).budget_states(
        _auth(ScopeKind.ORG, "o1"), scope=ScopeRef(ScopeKind.TEAM, "t1")
    )
    assert repo.subtree_calls == [(ScopeKind.TEAM, "t1", _PERIOD)]


async def test_filter_outside_scope_is_refused() -> None:
    repo = _FakeRepo(ancestry={(ScopeKind.ORG, "o1"): {ScopeKind.ORG: "o1"}}, states=[])
    with pytest.raises(ScopeNotAuthorized):
        await _handler(repo).budget_states(
            _auth(ScopeKind.USER, "u1"), scope=ScopeRef(ScopeKind.ORG, "o1")
        )
    assert repo.subtree_calls == []  # never reaches the enumeration


async def test_unknown_filter_node_is_refused_identically() -> None:
    repo = _FakeRepo(ancestry={}, states=[])  # resolve_scope_ancestry returns None
    with pytest.raises(ScopeNotAuthorized):
        await _handler(repo).budget_states(
            _auth(ScopeKind.ORG, "o1"), scope=ScopeRef(ScopeKind.TEAM, "ghost")
        )
    assert repo.subtree_calls == []


async def test_spend_rollup_defaults_to_credential_scope_and_current_period() -> None:
    repo = _FakeRepo(ancestry={}, states=[], groups=[SpendGroup("anthropic", 500)])
    view = await _handler(repo).spend_rollup(
        _auth(ScopeKind.USER, "u1"), group_by=GroupBy(GroupByKind.PROVIDER)
    )
    assert repo.spend_calls == [(ScopeKind.USER, "u1", _PERIOD, GroupBy(GroupByKind.PROVIDER))]
    assert view.period_start == _PERIOD
    assert view.groups == (SpendGroup("anthropic", 500),)


async def test_spend_rollup_uses_an_explicit_period_snapped_to_month() -> None:
    repo = _FakeRepo(ancestry={}, states=[], groups=[])
    await _handler(repo).spend_rollup(
        _auth(ScopeKind.USER, "u1"),
        group_by=GroupBy(GroupByKind.MODEL),
        period_start=datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
    )
    assert repo.spend_calls[0][2] == datetime(2026, 5, 1, tzinfo=UTC)  # snapped to month start


async def test_spend_rollup_coerces_a_naive_period_to_utc() -> None:
    repo = _FakeRepo(ancestry={}, states=[], groups=[])
    await _handler(repo).spend_rollup(
        _auth(ScopeKind.USER, "u1"),
        group_by=GroupBy(GroupByKind.MODEL),
        period_start=datetime(2026, 5, 17, 9, 30),  # tz-naive
    )
    assert repo.spend_calls[0][2] == datetime(2026, 5, 1, tzinfo=UTC)


async def test_spend_rollup_authorized_filter_reroots() -> None:
    repo = _FakeRepo(
        ancestry={(ScopeKind.TEAM, "t1"): {ScopeKind.ORG: "o1", ScopeKind.TEAM: "t1"}},
        states=[],
        groups=[],
    )
    await _handler(repo).spend_rollup(
        _auth(ScopeKind.ORG, "o1"),
        group_by=GroupBy(GroupByKind.PROVIDER),
        scope=ScopeRef(ScopeKind.TEAM, "t1"),
    )
    assert repo.spend_calls[0][0:2] == (ScopeKind.TEAM, "t1")


async def test_spend_rollup_filter_outside_scope_is_refused() -> None:
    repo = _FakeRepo(ancestry={(ScopeKind.ORG, "o1"): {ScopeKind.ORG: "o1"}}, states=[], groups=[])
    with pytest.raises(ScopeNotAuthorized):
        await _handler(repo).spend_rollup(
            _auth(ScopeKind.USER, "u1"),
            group_by=GroupBy(GroupByKind.PROVIDER),
            scope=ScopeRef(ScopeKind.ORG, "o1"),
        )
    assert repo.spend_calls == []
