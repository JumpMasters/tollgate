"""Tests for the domain error hierarchy."""

from __future__ import annotations

from tollgate.domain.errors import (
    BudgetNotFound,
    ConflictingBudgetScope,
    EnforcementUnavailable,
    IdempotencyKeyReuse,
    InsufficientBudget,
    ReservationNotHeld,
    TollgateError,
    UnknownModel,
)


def test_every_error_descends_from_base() -> None:
    for exc in (
        EnforcementUnavailable,
        InsufficientBudget,
        BudgetNotFound,
        UnknownModel,
        IdempotencyKeyReuse,
        ReservationNotHeld,
        ConflictingBudgetScope,
    ):
        assert issubclass(exc, TollgateError)


def test_insufficient_budget_names_the_scope() -> None:
    err = InsufficientBudget("user:alice")
    assert err.scope == "user:alice"
    assert "user:alice" in str(err)


def test_unknown_model_carries_the_pair() -> None:
    err = UnknownModel("openai", "gpt-x")
    assert err.provider == "openai"
    assert err.model == "gpt-x"
    assert "openai/gpt-x" in str(err)


def test_conflicting_budget_scope_names_the_node() -> None:
    err = ConflictingBudgetScope("team", "t1")
    assert err.scope_kind == "team"
    assert err.scope_id == "t1"
    assert "team:t1" in str(err)


def test_enforcement_unavailable_is_raisable() -> None:
    try:
        raise EnforcementUnavailable("datastore down")
    except TollgateError as exc:
        assert "datastore down" in str(exc)
