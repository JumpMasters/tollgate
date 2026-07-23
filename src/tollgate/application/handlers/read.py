"""ChargebackHandler: budget-state reads authorized to a credential's scope subtree.

A read carries a bearer credential like every command, but authorizes in the inverse
direction: rather than gating one target against the credential, it returns every budget node at or
below the credential's scope. With no filter the subtree is rooted at the credential's own node; an
optional ``scope`` filter re-roots it at a named sub-node, which must itself be at or below the
credential (checked with server-derived ancestry via :func:`authorizes`) or the read is refused
identically to an unknown node -- no existence leak. Off the command path: a read-only
connection, no transaction envelope, no idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tollgate.application.auth import AuthContext
from tollgate.application.ports import ChargebackReader, ChargebackRepository, Clock
from tollgate.domain.chargeback import BudgetStatesView, GroupBy, SpendRollup
from tollgate.domain.credentials import authorizes
from tollgate.domain.errors import ScopeNotAuthorized
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.scopes import ScopeKind, ScopeRef


class ChargebackHandler:
    """Answers budget-state reads over one read-only connection per call."""

    def __init__(self, *, reader: ChargebackReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    async def _resolve_root(
        self, repo: ChargebackRepository, auth: AuthContext, scope: ScopeRef | None
    ) -> tuple[ScopeKind, str]:
        """Resolve the subtree/rollup root, refusing a filter node outside the credential's scope.

        No filter -> the credential's own node (self-authorized). A filter must be at or below the
        credential (checked against server-derived ancestry); an unknown node and a foreign node are
        both refused identically as :class:`ScopeNotAuthorized` -- no existence leak.
        """
        if scope is None:
            return auth.credential.scope_kind, auth.credential.scope_id
        ancestry = await repo.resolve_scope_ancestry(scope.scope_kind, scope.scope_id)
        if ancestry is None or not authorizes(auth.credential, ancestry):
            raise ScopeNotAuthorized(f"{scope.scope_kind.value}:{scope.scope_id}")
        return scope.scope_kind, scope.scope_id

    async def budget_states(
        self, auth: AuthContext, *, scope: ScopeRef | None = None
    ) -> BudgetStatesView:
        """Return budget states at or below the credential's scope (optionally re-rooted at
        ``scope``).
        """
        period_start = calendar_month_start(self._clock.now())
        async with self._reader.begin() as repo:
            root_kind, root_id = await self._resolve_root(repo, auth, scope)
            states = await repo.subtree_states(root_kind, root_id, period_start)
        return BudgetStatesView(period_start=period_start, states=tuple(states))

    async def spend_rollup(
        self,
        auth: AuthContext,
        *,
        group_by: GroupBy,
        scope: ScopeRef | None = None,
        period_start: datetime | None = None,
    ) -> SpendRollup:
        """Return a node's realized spend for a period, grouped by ``group_by``."""
        raw = self._clock.now() if period_start is None else period_start
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=UTC)
        period = calendar_month_start(raw)
        async with self._reader.begin() as repo:
            root_kind, root_id = await self._resolve_root(repo, auth, scope)
            groups = await repo.spend_rollup(root_kind, root_id, period, group_by)
        return SpendRollup(period_start=period, groups=tuple(groups))
