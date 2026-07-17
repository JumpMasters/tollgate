"""ChargebackHandler: budget-state reads authorized to a credential's scope subtree (section 2,
5.0).

A read carries a bearer credential like every command (section 5.0), but authorizes in the inverse
direction: rather than gating one target against the credential, it returns every budget node at or
below the credential's scope. With no filter the subtree is rooted at the credential's own node; an
optional ``scope`` filter re-roots it at a named sub-node, which must itself be at or below the
credential (checked with server-derived ancestry via :func:`authorizes`) or the read is refused
identically to an unknown node -- no existence leak (section 5.0). Off the command path: a read-only
connection, no transaction envelope, no idempotency.
"""

from __future__ import annotations

from tollgate.application.auth import AuthContext
from tollgate.application.ports import ChargebackReader, Clock
from tollgate.domain.chargeback import BudgetStatesView
from tollgate.domain.credentials import authorizes
from tollgate.domain.errors import ScopeNotAuthorized
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.scopes import ScopeKind, ScopeRef


class ChargebackHandler:
    """Answers budget-state reads over one read-only connection per call."""

    def __init__(self, *, reader: ChargebackReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    async def budget_states(
        self, auth: AuthContext, *, scope: ScopeRef | None = None
    ) -> BudgetStatesView:
        """Return budget states at or below the credential's scope, or re-rooted at ``scope``."""
        period_start = calendar_month_start(self._clock.now())
        async with self._reader.begin() as repo:
            if scope is None:
                root_kind: ScopeKind = auth.credential.scope_kind
                root_id: str = auth.credential.scope_id
            else:
                ancestry = await repo.resolve_scope_ancestry(scope.scope_kind, scope.scope_id)
                if ancestry is None or not authorizes(auth.credential, ancestry):
                    raise ScopeNotAuthorized(f"{scope.scope_kind.value}:{scope.scope_id}")
                root_kind, root_id = scope.scope_kind, scope.scope_id
            states = await repo.subtree_states(root_kind, root_id, period_start)
        return BudgetStatesView(period_start=period_start, states=tuple(states))
