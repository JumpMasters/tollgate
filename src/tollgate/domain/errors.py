"""Typed errors raised by the domain and surfaced through the application.

These describe *business* outcomes (an exhausted budget, an unknown model) and
operational ones (the datastore being unreachable). Callers match on the type,
never on a string.
"""

from __future__ import annotations


class TollgateError(Exception):
    """Base class for every error Tollgate raises."""


class EnforcementUnavailable(TollgateError):
    """The spend gate could not reach its datastore, so no decision was made.

    Under the default fail-closed policy the call must not be dispatched.
    """


class InsufficientBudget(TollgateError):
    """A reserve was denied because a budget node lacked headroom.

    The binding node is named so the caller knows which limit was hit.
    """

    def __init__(self, scope: str) -> None:
        super().__init__(f"insufficient budget at {scope}")
        self.scope = scope


class BudgetNotFound(TollgateError):
    """No budget governs the requested scope."""


class ConflictingBudgetScope(TollgateError):
    """More than one distinct budget resolved for a single scope node.

    V1 enforces at most one budget per ``(scope_kind, scope_id)`` node (ADR 0025),
    and the schema constraint forbids two, so reaching this signals that invariant
    was violated upstream. The offending node is named.
    """

    def __init__(self, scope_kind: str, scope_id: str) -> None:
        super().__init__(f"multiple budgets resolved for scope {scope_kind}:{scope_id}")
        self.scope_kind = scope_kind
        self.scope_id = scope_id


class UnknownModel(TollgateError):
    """The requested (provider, model) pair is absent from the price book."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(f"unknown model: {provider}/{model}")
        self.provider = provider
        self.model = model


class IdempotencyKeyReuse(TollgateError):
    """An idempotency key was reused with a different command fingerprint."""


class ReservationNotHeld(TollgateError):
    """A terminal command targeted a reservation that is no longer held."""


class AuthenticationFailed(TollgateError):
    """A presented bearer token matched no active credential (§5.0).

    A missing, revoked, or otherwise unusable token is rejected identically, so the result never
    reveals which credentials exist.
    """


class ScopeNotAuthorized(TollgateError):
    """A credential tried to act on or read a node outside its scope (§5.0)."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"credential not authorized for {scope}")
        self.scope = scope
