"""Typed-error to HTTP mapping for the API surface (ADR 0031).

One exception handler covers the whole ``TollgateError`` taxonomy, so every
route and dependency surfaces domain failures through the same envelope:
``{"error": {"code": ..., "message": ...}}``. Budget denials map to 402 and
are never cached by the idempotency layer (section 5.1); authentication
failures carry ``WWW-Authenticate: Bearer`` per RFC 6750.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from tollgate.api.schemas import ErrorBody, ErrorEnvelope
from tollgate.domain.errors import (
    AmountOutOfRange,
    AuthenticationFailed,
    BalanceGuardViolation,
    BudgetNotFound,
    ConflictingBudgetScope,
    EnforcementUnavailable,
    IdempotencyKeyReuse,
    InsufficientBudget,
    ReservationNotHeld,
    ScopeNotAuthorized,
    TollgateError,
    UnknownModel,
)

#: Datastore connectivity/timeout failures that mean "no enforcement decision was made".
#: They are translated to :class:`EnforcementUnavailable` so a client SDK sees the documented
#: fail-closed 503 envelope (ADR 0031) instead of an off-contract 500. On the request path the
#: only I/O is the datastore, so a raw ``OSError`` (connection refused/reset, host unreachable,
#: DNS failure, and the ``ETIMEDOUT`` builtin ``TimeoutError``, which subclasses ``OSError``)
#: is a connect-time outage. ``OperationalError``/``InterfaceError`` cover failures on an
#: established connection (server-side statement timeout, dropped socket); the SQLAlchemy
#: ``TimeoutError`` covers pool-checkout exhaustion. Statement/constraint errors
#: (``IntegrityError``, ``ProgrammingError``, ...) are deliberately excluded: those are bugs,
#: not outages, and must keep failing loudly as 500s.
_UNAVAILABLE_ERRORS: Final = (
    OSError,
    OperationalError,
    InterfaceError,
    SQLAlchemyTimeoutError,
)

_FALLBACK: Final[tuple[int, str, str]] = (500, "internal_error", "internal error")

_MAPPING: Final[dict[type[TollgateError], tuple[int, str, str]]] = {
    AuthenticationFailed: (401, "authentication_failed", "invalid or missing bearer credential"),
    InsufficientBudget: (402, "insufficient_budget", "insufficient budget"),
    ScopeNotAuthorized: (403, "scope_not_authorized", "credential not authorized"),
    BudgetNotFound: (403, "budget_not_found", "no budget governs the request"),
    IdempotencyKeyReuse: (
        409,
        "idempotency_key_reuse",
        "idempotency key reused with a different command",
    ),
    ReservationNotHeld: (409, "reservation_not_held", "reservation is not held"),
    UnknownModel: (422, "unknown_model", "unknown (provider, model) pair"),
    AmountOutOfRange: (422, "amount_out_of_range", "amount out of representable range"),
    BalanceGuardViolation: (500, "balance_guard_violation", "balance guard matched no row"),
    ConflictingBudgetScope: (
        500,
        "conflicting_budget_scope",
        "conflicting budgets resolved for one scope node",
    ),
    EnforcementUnavailable: (503, "enforcement_unavailable", "enforcement datastore unavailable"),
}


def _error_response(exc: TollgateError) -> JSONResponse:
    status, code, default_message = _MAPPING.get(type(exc), _FALLBACK)
    envelope = ErrorEnvelope(error=ErrorBody(code=code, message=str(exc) or default_message))
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(status_code=status, content=envelope.model_dump(), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    """Install the domain-error and fail-closed datastore handlers on ``app``.

    The ``TollgateError`` handler covers every domain subtype; the connectivity handler
    translates a datastore outage into the 503 ``EnforcementUnavailable`` envelope (#62).
    """

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, TollgateError):  # pragma: no cover - registered for TollgateError
            raise exc
        return _error_response(exc)

    async def handle_unavailable(_: Request, exc: Exception) -> JSONResponse:
        # Fail closed: a connectivity/timeout failure means no decision was made. Do not echo
        # the driver's message (it can carry the DSN); use the envelope's default text.
        return _error_response(EnforcementUnavailable())

    app.add_exception_handler(TollgateError, handle)
    for exc_type in _UNAVAILABLE_ERRORS:
        app.add_exception_handler(exc_type, handle_unavailable)
