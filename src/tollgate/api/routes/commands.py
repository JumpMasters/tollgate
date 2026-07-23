"""The command routes: reserve, commit, cancel, extend, grace backfill.

Thin translations from the wire to the application handlers the composition
root placed on ``app.state``: parse the body, lift the Idempotency-Key header
into the command, call the handler with the authenticated context, and shape
the typed result. Domain errors propagate to the handler
installed by ``tollgate.api.errors`` (ADR 0031).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from tollgate.api.dependencies import RequestAuth
from tollgate.api.schemas import (
    MAX_STR_LEN,
    CancelRequest,
    CancelResponse,
    CommitRequest,
    CommitResponse,
    ErrorEnvelope,
    ExtendRequest,
    ExtendResponse,
    GraceBackfillRequest,
    GraceBackfillResponse,
    MeterRequest,
    MeterResponse,
    ReserveRequest,
    ReserveResponse,
    UsageBody,
)
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.meter import MeterHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import (
    CancelCommand,
    CommitCommand,
    ExtendCommand,
    GraceBackfillCommand,
    MeterCommand,
    ProviderUsage,
    ReserveCommand,
)
from tollgate.domain.ids import ProjectId, ReservationId

router = APIRouter(prefix="/v1")


def _normalize_idempotency_key(
    raw: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=MAX_STR_LEN)],
) -> str:
    """Trim surrounding whitespace so ``"k"`` and ``" k "`` are the same cache entry (#107).

    Only the raw length was bounded, so whitespace-padded variants of one key claimed distinct
    rows and defeated their own idempotency. Case is left intact — an idempotency key is an opaque
    client token, so lowercasing it could instead *collide* two keys a caller meant to keep apart.
    A key that is entirely whitespace trims to empty and is rejected.
    """
    key = raw.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    return key


IdempotencyKey = Annotated[str, Depends(_normalize_idempotency_key)]

#: Domain error statuses the command routes can return, documented with the error envelope
#: (ADR 0031). The request-validation 422 (and the domain UnknownModel 422 that shares it) is
#: left to FastAPI's own default documentation (ADR 0031, 0033).
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "missing or invalid bearer credential"},
    402: {"model": ErrorEnvelope, "description": "insufficient budget"},
    403: {"model": ErrorEnvelope, "description": "credential not authorized, or no budget"},
    409: {"model": ErrorEnvelope, "description": "idempotency key reuse, or reservation conflict"},
    500: {"model": ErrorEnvelope, "description": "internal error"},
    503: {"model": ErrorEnvelope, "description": "enforcement datastore unavailable"},
}

#: Only ``reserve`` can raise ``InsufficientBudget`` (402) — it is the sole admission gate. Every
#: other command records (meter, grace-backfill), reconciles (commit records overage rather than
#: denying), releases (cancel), or heartbeats (extend, touching no balance), so none can return a
#: 402 and none should advertise one (#100). This is the shared set minus insufficient-budget.
_NO_BUDGET_DENIAL_RESPONSES = {k: v for k, v in _ERROR_RESPONSES.items() if k != 402}


def _usage(body: UsageBody) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cached_input_tokens=body.cached_input_tokens,
        cache_creation_tokens=body.cache_creation_tokens,
    )


@router.post("/reserve", responses=_ERROR_RESPONSES)
async def reserve(
    request: Request,
    body: ReserveRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> ReserveResponse:
    """Reserve worst-case budget for a model call before it dispatches."""
    handler: ReserveHandler = request.app.state.reserve_handler
    command = ReserveCommand(
        idempotency_key=idempotency_key,
        provider=body.provider,
        model=body.model,
        input_bound_tokens=body.input_bound_tokens,
        max_output_tokens=body.max_output_tokens,
        cache_creation_bound_tokens=body.cache_creation_bound_tokens,
        labels=body.labels,
        project_id=None if body.project_id is None else ProjectId(body.project_id),
    )
    result = await handler.reserve(auth, command)
    return ReserveResponse(
        reservation_id=result.reservation_id,
        estimated_micro=result.estimated_micro,
        price_book_version=result.price_book_version,
        ttl_deadline=result.ttl_deadline,
    )


@router.post("/commit", responses=_NO_BUDGET_DENIAL_RESPONSES)
async def commit(
    request: Request,
    body: CommitRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> CommitResponse:
    """Reconcile a reservation to provider-reported usage."""
    handler: CommitHandler = request.app.state.commit_handler
    command = CommitCommand(
        idempotency_key=idempotency_key,
        reservation_id=ReservationId(body.reservation_id),
        usage=_usage(body.usage),
    )
    result = await handler.commit(auth, command)
    return CommitResponse(
        reservation_id=result.reservation_id,
        committed_micro=result.committed_micro,
        overage_micro=result.overage_micro,
    )


@router.post("/cancel", responses=_NO_BUDGET_DENIAL_RESPONSES)
async def cancel(
    request: Request,
    body: CancelRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> CancelResponse:
    """Release a reservation whose call failed before incurring usage."""
    handler: CancelHandler = request.app.state.cancel_handler
    command = CancelCommand(
        idempotency_key=idempotency_key,
        reservation_id=ReservationId(body.reservation_id),
    )
    result = await handler.cancel(auth, command)
    return CancelResponse(
        reservation_id=result.reservation_id,
        released_micro=result.released_micro,
    )


@router.post("/extend", responses=_NO_BUDGET_DENIAL_RESPONSES)
async def extend(request: Request, body: ExtendRequest, auth: RequestAuth) -> ExtendResponse:
    """Heartbeat a held reservation; no Idempotency-Key - extend is monotonic."""
    handler: ExtendHandler = request.app.state.extend_handler
    command = ExtendCommand(reservation_id=ReservationId(body.reservation_id))
    result = await handler.extend(auth, command)
    return ExtendResponse(reservation_id=result.reservation_id, ttl_deadline=result.ttl_deadline)


@router.post("/grace-backfill", responses=_NO_BUDGET_DENIAL_RESPONSES)
async def grace_backfill(
    request: Request,
    body: GraceBackfillRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> GraceBackfillResponse:
    """Backfill spend incurred while enforcement was unreachable (ADR 0030)."""
    handler: GraceBackfillHandler = request.app.state.grace_backfill_handler
    command = GraceBackfillCommand(
        idempotency_key=idempotency_key,
        provider=body.provider,
        model=body.model,
        usage=_usage(body.usage),
        project_id=None if body.project_id is None else ProjectId(body.project_id),
    )
    result = await handler.backfill(auth, command)
    return GraceBackfillResponse(
        actual_micro=result.actual_micro,
        price_book_version=result.price_book_version,
    )


@router.post("/meter", responses=_NO_BUDGET_DENIAL_RESPONSES)
async def meter(
    request: Request,
    body: MeterRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> MeterResponse:
    """Record already-incurred, provider-reported spend; never denies (ADR 0037)."""
    handler: MeterHandler = request.app.state.meter_handler
    command = MeterCommand(
        idempotency_key=idempotency_key,
        provider=body.provider,
        model=body.model,
        usage=_usage(body.usage),
        labels=body.labels,
        project_id=None if body.project_id is None else ProjectId(body.project_id),
        truncated=body.truncated,
    )
    result = await handler.meter(auth, command)
    return MeterResponse(
        actual_micro=result.actual_micro, price_book_version=result.price_book_version
    )
