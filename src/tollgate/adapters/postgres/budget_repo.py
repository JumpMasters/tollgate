"""PostgresBudgetRepository: the budget nodes a reserve gates against (§4, §5.3).

``find_ancestry_budgets`` returns the budgets that exist on the principal's org / team / user nodes
(a single ``(scope_kind, scope_id) IN (...)`` lookup — ancestry scopes without a budget are simply
absent). ``find_project`` left-joins ``project`` to its ``project``-scoped budget so a named project
resolves to its org (for the server-derived authorization ancestry) plus its budget node if it has
one, or ``None`` when no such project exists. Explicit async SQLAlchemy Core, no ORM; never imports
``application`` and satisfies the port structurally.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import budget, project
from tollgate.domain.credentials import Principal
from tollgate.domain.ids import BudgetId, OrgId, ProjectId
from tollgate.domain.scopes import BudgetNode, ResolvedProject, ScopeKind


class PostgresBudgetRepository:
    """Budget-node lookups for the reserve path on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        """Return the budgets on the principal's ``org`` / ``team`` / ``user`` nodes (§5.3)."""
        wanted = [
            (ScopeKind.ORG.value, principal.org_id),
            (ScopeKind.TEAM.value, principal.team_id),
            (ScopeKind.USER.value, principal.user_id),
        ]
        rows = (
            await self._conn.execute(
                select(budget.c.budget_id, budget.c.scope_kind, budget.c.scope_id).where(
                    tuple_(budget.c.scope_kind, budget.c.scope_id).in_(wanted)
                )
            )
        ).all()
        return [
            BudgetNode(BudgetId(row.budget_id), ScopeKind(row.scope_kind), row.scope_id)
            for row in rows
        ]

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        """Resolve a named project to its org and optional budget node, or ``None`` (§5.0)."""
        row = (
            await self._conn.execute(
                select(project.c.org_id, budget.c.budget_id)
                .select_from(
                    project.outerjoin(
                        budget,
                        and_(
                            budget.c.scope_kind == ScopeKind.PROJECT.value,
                            budget.c.scope_id == project.c.project_id,
                        ),
                    )
                )
                .where(project.c.project_id == project_id)
            )
        ).first()
        if row is None:
            return None
        node = (
            BudgetNode(BudgetId(row.budget_id), ScopeKind.PROJECT, project_id)
            if row.budget_id is not None
            else None
        )
        return ResolvedProject(org_id=OrgId(row.org_id), budget=node)
