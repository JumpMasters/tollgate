"""SDK client exceptions, mapped from the ADR-0031 error envelope.

The SDK is a decoupled client: it defines its own exception taxonomy rather than importing the
server's domain errors, so a caller catches
``tollgate.adapters.integrations.sdk.EnforcementUnavailable`` without pulling in server internals.
``EnforcementUnavailable`` is the fail-closed signal a caller keys degraded-mode behaviour on
(spec §5.6): it covers both a 503 from the control plane and any connectivity/timeout failure
reaching it.
"""

from __future__ import annotations


class TollgateApiError(Exception):
    """Base for every error the SDK raises for a non-2xx (or unreachable) control plane."""

    def __init__(self, message: str, *, status: int, code: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class AuthenticationFailed(TollgateApiError):
    """The bearer token was missing, invalid, or revoked (401)."""


class NotAuthorized(TollgateApiError):
    """The credential may not act on the requested scope, or no budget governs it (403)."""


class BudgetDenied(TollgateApiError):
    """A budget node lacked headroom; the call must not dispatch (402)."""


class BudgetNotFound(NotAuthorized):
    """No budget governs the request (403 budget_not_found) — a specific :class:`NotAuthorized`."""


class IdempotencyKeyReuse(TollgateApiError):
    """The idempotency key was reused with a different command (409)."""


class ReservationNotHeld(TollgateApiError):
    """The targeted reservation is no longer held (409)."""


class UnknownModel(TollgateApiError):
    """The (provider, model) pair is not in the price book (422)."""


class InvalidRequest(TollgateApiError):
    """The request was rejected as unprocessable (422: validation, bounds, zero estimate)."""


class EnforcementUnavailable(TollgateApiError):
    """No enforcement decision could be made — fail closed, do not dispatch (503 or unreachable)."""


class InternalError(TollgateApiError):
    """The control plane returned a 5xx tagged with its generic ``internal_error`` code."""


_BY_CODE: dict[str, type[TollgateApiError]] = {
    "authentication_failed": AuthenticationFailed,
    "insufficient_budget": BudgetDenied,
    "scope_not_authorized": NotAuthorized,
    "budget_not_found": BudgetNotFound,
    "idempotency_key_reuse": IdempotencyKeyReuse,
    "reservation_not_held": ReservationNotHeld,
    "unknown_model": UnknownModel,
    "amount_out_of_range": InvalidRequest,
    "non_positive_estimate": InvalidRequest,
    "enforcement_unavailable": EnforcementUnavailable,
    "internal_error": InternalError,
    "conflicting_budget_scope": InternalError,
    "balance_guard_violation": InternalError,
}


def error_for(status: int, code: str | None, message: str) -> TollgateApiError:
    """Map a control-plane response (status + envelope code) to a typed SDK exception.

    A known code wins; otherwise a 5xx (or an unmapped status) fails closed to
    :class:`EnforcementUnavailable`, and any remaining 4xx is an :class:`InvalidRequest`.
    """
    mapped = _BY_CODE.get(code or "")
    if mapped is not None:
        return mapped(message, status=status, code=code)
    if status >= 500:
        return EnforcementUnavailable(message, status=status, code=code)
    return InvalidRequest(message, status=status, code=code)
