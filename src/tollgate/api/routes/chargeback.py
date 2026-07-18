"""The chargeback read routes: GET /v1/budgets and GET /v1/spend (section 2, 5.0, ADR 0032, 0033).

``GET /v1/budgets`` authorizes to budgets at or below the bearer credential's scope; an optional
``?scope=<kind>:<id>`` re-roots the returned subtree at a named node, refused identically to an
unknown one (no existence leak). ``GET /v1/spend?group_by=<dim>`` returns one scope node's
realized spend for a period, grouped by provider, model, or a label key, over the same ``scope``
re-rooting and authorization rules; a malformed ``group_by`` is a 422. Off the command path: both
are GETs, no Idempotency-Key, a read-only connection. Domain errors propagate to the handler
installed by ``tollgate.api.errors``; a malformed ``scope`` is a 422.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from tollgate.api.dependencies import RequestAuth
from tollgate.api.schemas import (
    BudgetAlertState,
    BudgetStateResponse,
    BudgetStatesResponse,
    ErrorEnvelope,
    SpendGroupResponse,
    SpendRollupResponse,
)
from tollgate.application.handlers.read import ChargebackHandler
from tollgate.domain.chargeback import (
    BudgetState,
    BudgetStatesView,
    SpendRollup,
    crossed_thresholds,
    parse_group_by,
    remaining_micro,
    utilization_pct,
)
from tollgate.domain.scopes import ScopeKind, ScopeRef

router = APIRouter(prefix="/v1")

_SCOPE_KINDS = {kind.value for kind in ScopeKind}

#: Domain error statuses the chargeback reads can return, documented with the error envelope
#: (ADR 0031). The malformed-scope/group-by 422 is left to FastAPI's own default documentation.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "missing or invalid bearer credential"},
    403: {"model": ErrorEnvelope, "description": "credential not authorized, or no budget"},
    500: {"model": ErrorEnvelope, "description": "internal error"},
    503: {"model": ErrorEnvelope, "description": "enforcement datastore unavailable"},
}


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
            for threshold in state.alert_thresholds_pct
        ],
    )


@router.get("/budgets", responses=_ERROR_RESPONSES)
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


def _spend_response(rollup: SpendRollup, group_by: str) -> SpendRollupResponse:
    return SpendRollupResponse(
        period_start=rollup.period_start,
        group_by=group_by,
        groups=[
            SpendGroupResponse(group=g.group, spend_micro=g.spend_micro) for g in rollup.groups
        ],
    )


@router.get("/spend", responses=_ERROR_RESPONSES)
async def spend(
    request: Request,
    auth: RequestAuth,
    group_by: str,
    scope: str | None = None,
    period_start: datetime | None = None,
) -> SpendRollupResponse:
    """Realized spend for a scope node, grouped by a dimension, for one period (section 2, 5.0)."""
    parsed = parse_group_by(group_by)
    if parsed is None:
        raise HTTPException(
            status_code=422,
            detail="group_by must be 'provider', 'model', or 'label:<key>'",
        )
    handler: ChargebackHandler = request.app.state.chargeback_handler
    rollup = await handler.spend_rollup(
        auth, group_by=parsed, scope=_parse_scope(scope), period_start=period_start
    )
    return _spend_response(rollup, group_by)
