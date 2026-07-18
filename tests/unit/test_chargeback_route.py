"""Unit tests for the chargeback route helpers and composition wiring (section 2, ADR 0032)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from tollgate.api.routes.chargeback import _node_response, _parse_scope, _spend_response
from tollgate.app import build_app
from tollgate.config.settings import Settings
from tollgate.domain.chargeback import BudgetState, SpendGroup, SpendRollup
from tollgate.domain.ids import BudgetId
from tollgate.domain.invariants import Balance
from tollgate.domain.scopes import ScopeKind, ScopeRef


def test_parse_scope_none_passes_through() -> None:
    assert _parse_scope(None) is None


def test_parse_scope_reads_kind_and_id() -> None:
    assert _parse_scope("team:t1") == ScopeRef(ScopeKind.TEAM, "t1")
    assert _parse_scope("project:p-1") == ScopeRef(ScopeKind.PROJECT, "p-1")


@pytest.mark.parametrize("bad", ["nokind", "unknown:x", "team:", ":t1", "", "USER:u1"])
def test_parse_scope_rejects_malformed_values_as_422(bad: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _parse_scope(bad)
    assert excinfo.value.status_code == 422


def test_node_response_maps_amounts_utilization_and_alert_flags() -> None:
    state = BudgetState(
        budget_id=BudgetId("b-user"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        balance=Balance(
            limit_micro=1_000, reserved_micro=500, committed_micro=300, overage_micro=0
        ),
        alert_thresholds_pct=(50, 80, 95),
    )
    response = _node_response(state)
    assert response.scope_kind == "user"
    assert response.scope_id == "u1"
    assert response.limit_micro == 1_000
    assert response.reserved_micro == 500
    assert response.committed_micro == 300
    assert response.overage_micro == 0
    assert response.remaining_micro == 200
    assert response.utilization_pct == 80
    assert [(a.threshold_pct, a.crossed) for a in response.alerts] == [
        (50, True),
        (80, True),
        (95, False),
    ]


async def test_build_app_wires_the_chargeback_route_and_handler() -> None:
    app = build_app(Settings(token_hash_secret="unit-secret"))
    try:
        assert hasattr(app.state, "chargeback_handler")
        assert "/v1/budgets" in app.openapi()["paths"]
        assert "/v1/spend" in app.openapi()["paths"]
    finally:
        await app.state.engine.dispose()


async def test_openapi_documents_the_error_envelope_for_domain_statuses() -> None:
    """The error envelope is discoverable in the schema, not just the success body (ADR 0031)."""
    app = build_app(Settings(token_hash_secret="unit-secret"))
    try:
        schema = app.openapi()
        budgets_responses = schema["paths"]["/v1/budgets"]["get"]["responses"]
        assert "403" in budgets_responses
        reserve_responses = schema["paths"]["/v1/reserve"]["post"]["responses"]
        assert "402" in reserve_responses
    finally:
        await app.state.engine.dispose()


def test_spend_response_maps_groups_and_echoes_dimension() -> None:
    from datetime import UTC, datetime

    rollup = SpendRollup(
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        groups=(SpendGroup("anthropic", 500), SpendGroup(None, 100)),
    )
    response = _spend_response(rollup, "provider")
    assert response.group_by == "provider"
    assert response.period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert [(g.group, g.spend_micro) for g in response.groups] == [("anthropic", 500), (None, 100)]
