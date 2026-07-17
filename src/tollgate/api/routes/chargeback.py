"""The chargeback read route: GET /v1/budgets (section 2, 5.0, ADR 0032).

Authorizes to budgets at or below the bearer credential's scope; an optional ``?scope=<kind>:<id>``
re-roots the returned subtree at a named node, refused identically to an unknown one (no existence
leak). Off the command path: a GET, no Idempotency-Key, a read-only connection. Domain errors
propagate to the handler installed by ``tollgate.api.errors``; a malformed ``scope`` is a 422.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from tollgate.api.dependencies import RequestAuth
from tollgate.api.schemas import BudgetAlertState, BudgetStateResponse, BudgetStatesResponse
from tollgate.application.handlers.read import ChargebackHandler
from tollgate.domain.chargeback import (
    BudgetState,
    BudgetStatesView,
    crossed_thresholds,
    remaining_micro,
    utilization_pct,
)
from tollgate.domain.scopes import ScopeKind, ScopeRef

router = APIRouter(prefix="/v1")

_SCOPE_KINDS = {kind.value for kind in ScopeKind}


def _parse_scope(scope: str | None) -> ScopeRef | None:
    """Parse ``<kind>:<id>`` into a :class:`ScopeRef`; ``None`` passes through. 422 on malformed."""
    if scope is None:
        return None
    kind, separator, scope_id = scope.partition(":")
    if not separator or kind not in _SCOPE_KINDS or not scope_id:
        raise HTTPException(
            status_code=422,
            detail="scope must be '<kind>:<id>' where kind is one of org, team, user, project",
        )
    return ScopeRef(scope_kind=ScopeKind(kind), scope_id=scope_id)


def _node_response(state: BudgetState) -> BudgetStateResponse:
    crossed = set(crossed_thresholds(state))
    return BudgetStateResponse(
        scope_kind=state.scope_kind.value,
        scope_id=state.scope_id,
        limit_micro=state.balance.limit_micro,
        reserved_micro=state.balance.reserved_micro,
        committed_micro=state.balance.committed_micro,
        overage_micro=state.balance.overage_micro,
        remaining_micro=remaining_micro(state),
        utilization_pct=utilization_pct(state),
        alerts=[
            BudgetAlertState(threshold_pct=threshold, crossed=threshold in crossed)
            for threshold in sorted(state.alert_thresholds_pct)
        ],
    )


@router.get("/budgets")
async def budgets(
    request: Request, auth: RequestAuth, scope: str | None = None
) -> BudgetStatesResponse:
    """Return budget state at or below the credential's scope.

    Section 2; optional ``?scope=`` filter.
    """
    handler: ChargebackHandler = request.app.state.chargeback_handler
    view: BudgetStatesView = await handler.budget_states(auth, scope=_parse_scope(scope))
    return BudgetStatesResponse(
        period_start=view.period_start,
        budgets=[_node_response(state) for state in view.states],
    )
