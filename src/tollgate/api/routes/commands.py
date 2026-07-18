"""The command routes: reserve, commit, cancel, extend, grace backfill (sections 4-5).

Thin translations from the wire to the application handlers the composition
root placed on ``app.state``: parse the body, lift the Idempotency-Key header
into the command (section 5.1), call the handler with the authenticated
context, and shape the typed result. Domain errors propagate to the handler
installed by ``tollgate.api.errors`` (ADR 0031).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from tollgate.api.dependencies import RequestAuth
from tollgate.api.schemas import (
    CancelRequest,
    CancelResponse,
    CommitRequest,
    CommitResponse,
    ExtendRequest,
    ExtendResponse,
    GraceBackfillRequest,
    GraceBackfillResponse,
    ReserveRequest,
    ReserveResponse,
    UsageBody,
)
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import (
    CancelCommand,
    CommitCommand,
    ExtendCommand,
    GraceBackfillCommand,
    ProviderUsage,
    ReserveCommand,
)
from tollgate.domain.ids import ProjectId, ReservationId

router = APIRouter(prefix="/v1")

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1)]


def _usage(body: UsageBody) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cached_input_tokens=body.cached_input_tokens,
    )


@router.post("/reserve")
async def reserve(
    request: Request,
    body: ReserveRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> ReserveResponse:
    """Reserve worst-case budget for a model call before it dispatches (section 4)."""
    handler: ReserveHandler = request.app.state.reserve_handler
    command = ReserveCommand(
        idempotency_key=idempotency_key,
        provider=body.provider,
        model=body.model,
        input_bound_tokens=body.input_bound_tokens,
        max_output_tokens=body.max_output_tokens,
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


@router.post("/commit")
async def commit(
    request: Request,
    body: CommitRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> CommitResponse:
    """Reconcile a reservation to provider-reported usage (section 4)."""
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


@router.post("/cancel")
async def cancel(
    request: Request,
    body: CancelRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> CancelResponse:
    """Release a reservation whose call failed before incurring usage (section 4)."""
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


@router.post("/extend")
async def extend(request: Request, body: ExtendRequest, auth: RequestAuth) -> ExtendResponse:
    """Heartbeat a held reservation; no Idempotency-Key - extend is monotonic (section 4)."""
    handler: ExtendHandler = request.app.state.extend_handler
    command = ExtendCommand(reservation_id=ReservationId(body.reservation_id))
    result = await handler.extend(auth, command)
    return ExtendResponse(reservation_id=result.reservation_id, ttl_deadline=result.ttl_deadline)


@router.post("/grace-backfill")
async def grace_backfill(
    request: Request,
    body: GraceBackfillRequest,
    auth: RequestAuth,
    idempotency_key: IdempotencyKey,
) -> GraceBackfillResponse:
    """Backfill spend incurred while enforcement was unreachable (section 5.6, ADR 0030)."""
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
