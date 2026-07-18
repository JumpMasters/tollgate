"""Read-side budget state for the chargeback API (section 2, 5.0).

``PostgresChargebackRepository.subtree_states`` enumerates every budget at or below a scope
node -- the fixed org -> team -> user hierarchy plus the orthogonal project axis, so a bounded
set of ``IN`` sub-selects, no recursive CTE -- and LEFT JOINs ``budget_balance`` for the period,
synthesizing zero state (against the budget's ``hard_limit_micro``) for a node that has had no
activity this period (no balance row exists yet; a read never seeds one).
``resolve_scope_ancestry`` returns a node's server-derived ancestry map for the authorization
check (section 5.0). ``spend_rollup`` sums one node's own budget ledger rows for a period,
grouped by provider, model, or a label key -- joined on ``budget`` for that single node so a
subtree union never double-counts shared spend (section 2). ``PostgresChargebackReader`` hands
out a repository bound to a fresh read-only connection per read. Explicit async SQLAlchemy Core,
no ORM; satisfies the ports structurally without importing ``application``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tollgate.adapters.postgres.schema import (
    budget,
    budget_alert,
    budget_balance,
    ledger,
    org,
    project,
    reservation,
    team,
    user_principal,
)
from tollgate.domain.chargeback import BudgetState, GroupBy, GroupByKind, SpendGroup
from tollgate.domain.ids import BudgetId
from tollgate.domain.invariants import Balance
from tollgate.domain.scopes import ScopeKind, scope_rank


def _subtree_predicate(scope_kind: ScopeKind, scope_id: str) -> ColumnElement[bool]:
    """Budgets at or below ``(scope_kind, scope_id)`` over the fixed hierarchy + project axis.

    Section 5.0. ORG covers its teams, those teams' users, and its projects; TEAM covers itself
    and its users (a project has no team ancestor, so it is excluded); USER and PROJECT are
    leaves.
    """
    b = budget.c
    if scope_kind is ScopeKind.ORG:
        teams = select(team.c.team_id).where(team.c.org_id == scope_id)
        users = select(user_principal.c.user_id).where(user_principal.c.team_id.in_(teams))
        projects = select(project.c.project_id).where(project.c.org_id == scope_id)
        return or_(
            and_(b.scope_kind == ScopeKind.ORG.value, b.scope_id == scope_id),
            and_(b.scope_kind == ScopeKind.TEAM.value, b.scope_id.in_(teams)),
            and_(b.scope_kind == ScopeKind.USER.value, b.scope_id.in_(users)),
            and_(b.scope_kind == ScopeKind.PROJECT.value, b.scope_id.in_(projects)),
        )
    if scope_kind is ScopeKind.TEAM:
        users = select(user_principal.c.user_id).where(user_principal.c.team_id == scope_id)
        return or_(
            and_(b.scope_kind == ScopeKind.TEAM.value, b.scope_id == scope_id),
            and_(b.scope_kind == ScopeKind.USER.value, b.scope_id.in_(users)),
        )
    return and_(b.scope_kind == scope_kind.value, b.scope_id == scope_id)


class PostgresChargebackRepository:
    """Budget-state reads on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def subtree_states(
        self, scope_kind: ScopeKind, scope_id: str, period_start: datetime
    ) -> Sequence[BudgetState]:
        b, bal = budget.c, budget_balance.c
        stmt = (
            select(
                b.budget_id,
                b.scope_kind,
                b.scope_id,
                b.hard_limit_micro,
                bal.limit_micro,
                bal.reserved_micro,
                bal.committed_micro,
                bal.overage_micro,
            )
            .select_from(
                budget.outerjoin(
                    budget_balance,
                    and_(bal.budget_id == b.budget_id, bal.period_start == period_start),
                )
            )
            .where(_subtree_predicate(scope_kind, scope_id))
        )
        rows = (await self._conn.execute(stmt)).all()
        if not rows:
            return []
        thresholds = await self._alert_thresholds([BudgetId(row.budget_id) for row in rows])
        states = [
            BudgetState(
                budget_id=BudgetId(row.budget_id),
                scope_kind=ScopeKind(row.scope_kind),
                scope_id=row.scope_id,
                balance=Balance(
                    limit_micro=(
                        row.hard_limit_micro if row.limit_micro is None else row.limit_micro
                    ),
                    reserved_micro=0 if row.reserved_micro is None else row.reserved_micro,
                    committed_micro=0 if row.committed_micro is None else row.committed_micro,
                    overage_micro=0 if row.overage_micro is None else row.overage_micro,
                ),
                alert_thresholds_pct=thresholds.get(BudgetId(row.budget_id), ()),
            )
            for row in rows
        ]
        states.sort(key=lambda s: (scope_rank(s.scope_kind), s.scope_id))
        return states

    async def _alert_thresholds(
        self, budget_ids: Sequence[BudgetId]
    ) -> Mapping[BudgetId, tuple[int, ...]]:
        a = budget_alert.c
        rows = (
            await self._conn.execute(
                select(a.budget_id, a.threshold_pct).where(a.budget_id.in_(budget_ids))
            )
        ).all()
        grouped: dict[BudgetId, list[int]] = {}
        for row in rows:
            grouped.setdefault(BudgetId(row.budget_id), []).append(row.threshold_pct)
        return {bid: tuple(sorted(values)) for bid, values in grouped.items()}

    async def resolve_scope_ancestry(
        self, scope_kind: ScopeKind, scope_id: str
    ) -> Mapping[ScopeKind, str] | None:
        if scope_kind is ScopeKind.ORG:
            row = (
                await self._conn.execute(select(org.c.org_id).where(org.c.org_id == scope_id))
            ).first()
            return None if row is None else {ScopeKind.ORG: row.org_id}
        if scope_kind is ScopeKind.TEAM:
            row = (
                await self._conn.execute(select(team.c.org_id).where(team.c.team_id == scope_id))
            ).first()
            return None if row is None else {ScopeKind.ORG: row.org_id, ScopeKind.TEAM: scope_id}
        if scope_kind is ScopeKind.USER:
            row = (
                await self._conn.execute(
                    select(user_principal.c.team_id, team.c.org_id)
                    .select_from(
                        user_principal.join(team, team.c.team_id == user_principal.c.team_id)
                    )
                    .where(user_principal.c.user_id == scope_id)
                )
            ).first()
            return (
                None
                if row is None
                else {
                    ScopeKind.ORG: row.org_id,
                    ScopeKind.TEAM: row.team_id,
                    ScopeKind.USER: scope_id,
                }
            )
        row = (
            await self._conn.execute(
                select(project.c.org_id).where(project.c.project_id == scope_id)
            )
        ).first()
        return None if row is None else {ScopeKind.ORG: row.org_id, ScopeKind.PROJECT: scope_id}

    async def spend_rollup(
        self,
        scope_kind: ScopeKind,
        scope_id: str,
        period_start: datetime,
        group_by: GroupBy,
    ) -> Sequence[SpendGroup]:
        led, bud, res = ledger.c, budget.c, reservation.c
        spend = func.sum(led.delta_committed_micro + led.delta_overage_micro)
        node = and_(
            bud.budget_id == led.budget_id,
            bud.scope_kind == scope_kind.value,
            bud.scope_id == scope_id,
        )
        grouping: ColumnElement[Any]
        if group_by.kind is GroupByKind.PROVIDER:
            grouping = led.provider
            source = ledger.join(budget, node)
        elif group_by.kind is GroupByKind.MODEL:
            grouping = res.model
            source = ledger.join(budget, node).outerjoin(
                reservation, res.reservation_id == led.reservation_id
            )
        else:  # LABEL: labels ->> :key (parse_group_by guarantees a non-empty key)
            key = group_by.label_key or ""
            grouping = res.labels.op("->>")(key)
            source = ledger.join(budget, node).outerjoin(
                reservation, res.reservation_id == led.reservation_id
            )
        stmt = (
            select(grouping.label("grp"), spend.label("spend_micro"))
            .select_from(source)
            .where(led.period_start == period_start)
            .group_by(grouping)
            .having(spend != 0)
            .order_by(spend.desc(), grouping.asc().nulls_last())
        )
        rows = (await self._conn.execute(stmt)).all()
        return [SpendGroup(group=row.grp, spend_micro=int(row.spend_micro)) for row in rows]


class PostgresChargebackReader:
    """Opens a fresh read-only connection per read and yields a repository bound to it."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[PostgresChargebackRepository]:
        async with self._engine.connect() as conn:
            yield PostgresChargebackRepository(conn)
