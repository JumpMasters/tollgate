"""The guard: reserve worst-case budget before dispatch, commit/cancel in a finally."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from tollgate.adapters.integrations.sdk.client import ProviderUsage, TollgateClient
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import EnforcementUnavailable, InvalidRequest
from tollgate.adapters.integrations.sdk.tokenizer import Tokenizer, input_bound_tokens

logger = logging.getLogger(__name__)


class GuardedCall:
    """Yielded inside ``async with guard(...)``.

    Carries the reservation identity and is where a caller records provider usage.
    """

    def __init__(self, reservation_id: str, estimated_micro: int) -> None:
        self.reservation_id = reservation_id
        self.estimated_micro = estimated_micro
        self._usage: ProviderUsage | None = None

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Record the provider-reported usage to reconcile on commit."""
        self._usage = ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    @property
    def usage(self) -> ProviderUsage | None:
        return self._usage


def _default_key() -> str:
    return uuid4().hex


async def _heartbeat(client: TollgateClient, reservation_id: str, interval: float) -> None:
    """Extend the reservation's TTL every ``interval`` seconds until cancelled."""
    while True:
        await asyncio.sleep(interval)
        try:
            await client.extend(reservation_id=reservation_id)
        except Exception:  # a reaper is the backstop; a missed heartbeat must not break the call
            logger.warning(
                "heartbeat extend failed for %s; the reaper is the backstop", reservation_id
            )


async def _stop(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _finalize(
    client: TollgateClient,
    call: GuardedCall,
    *,
    provider: str,
    model: str,
    project: str | None,
    labels: dict[str, str] | None,
    new_key: Callable[[], str],
) -> None:
    """Resolve the reservation on exit: commit recorded usage, else cancel.

    Recorded usage is real, provider-billed spend, so it is always committed — even when the body
    raised after ``record_usage`` (#94); only a call that consumed nothing is cancelled.

    A commit that fails *transiently* (``EnforcementUnavailable`` — the control plane blipped or
    is unreachable) must not strand the reservation for the TTL reaper to release, which would
    silently under-charge the real spend (#93). It falls back to a durable ``meter`` under a
    deterministic, retry-safe key, biasing to the safe direction for a spend-control plane: the
    reaper later releases the now-redundant estimate, so at worst spend is momentarily over-counted
    rather than lost. The fallback is scoped to ``EnforcementUnavailable`` on purpose — a *definite*
    rejection (e.g. the reservation already settled) is not a blip, and metering it again could
    double-charge, so it propagates. Only if the meter fallback *also* fails does the typed commit
    error surface, so the caller learns the spend went unrecorded instead of assuming success.
    """
    usage = call.usage
    if usage is None:
        await client.cancel(reservation_id=call.reservation_id, idempotency_key=new_key())
        return
    try:
        await client.commit(
            reservation_id=call.reservation_id, usage=usage, idempotency_key=new_key()
        )
    except EnforcementUnavailable as commit_error:
        logger.warning(
            "commit unavailable for %s; recording spend via meter fallback", call.reservation_id
        )
        try:
            await client.meter(
                provider=provider,
                model=model,
                usage=usage,
                idempotency_key=f"commit-fallback-{call.reservation_id}",
                project=project,
                labels=labels,
            )
        except Exception:
            logger.error(
                "commit and meter fallback both failed for %s; real spend was not recorded",
                call.reservation_id,
            )
            raise commit_error from None


@asynccontextmanager
async def guard(
    client: TollgateClient,
    *,
    config: SdkConfig,
    tokenizer: Tokenizer,
    provider: str,
    model: str,
    prompt: str,
    max_output_tokens: int | None = None,
    project: str | None = None,
    labels: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    new_key: Callable[[], str] = _default_key,
) -> AsyncIterator[GuardedCall]:
    """Reserve before dispatch; commit on clean exit with usage, else cancel.

    A denied reserve (``BudgetDenied``/``NotAuthorized``) or an unreachable control plane
    (``EnforcementUnavailable``, fail-closed) raises *before* the body runs, so the model call
    never dispatches. On exit the reservation is always resolved by :func:`_finalize`: recorded
    usage is real, provider-billed spend and is committed whether the body exited cleanly or
    raised; only a call that recorded no usage is cancelled.

    ``new_key`` must return a fresh value on every call: reserve (when no explicit
    ``idempotency_key`` is given), commit, and cancel each call it once, and a constant generator
    would collide the reserve and commit keys into a 409 ``idempotency_key_reuse``.
    """
    if max_output_tokens is None:
        if config.strict_uncapped:
            raise InvalidRequest(
                "max_output_tokens is required in strict mode", status=422, code=None
            )
        max_output_tokens = config.default_max_output_tokens

    bound = input_bound_tokens(
        tokenizer, prompt, provider_margin_tokens=config.provider_margin_tokens
    )
    reserved = await client.reserve(
        provider=provider,
        model=model,
        input_bound_tokens=bound,
        max_output_tokens=max_output_tokens,
        idempotency_key=idempotency_key or new_key(),
        project=project,
        labels=labels,
    )
    call = GuardedCall(reserved.reservation_id, reserved.estimated_micro)
    heartbeat: asyncio.Task[None] | None = None
    if config.heartbeat_interval_seconds > 0:
        heartbeat = asyncio.create_task(
            _heartbeat(client, call.reservation_id, config.heartbeat_interval_seconds)
        )
    try:
        yield call
    except BaseException:
        await _stop(heartbeat)
        # Best-effort on the failure path: resolving the reservation must never mask the body
        # exception the caller is already handling.
        with contextlib.suppress(Exception):
            await _finalize(
                client,
                call,
                provider=provider,
                model=model,
                project=project,
                labels=labels,
                new_key=new_key,
            )
        raise
    else:
        await _stop(heartbeat)
        await _finalize(
            client,
            call,
            provider=provider,
            model=model,
            project=project,
            labels=labels,
            new_key=new_key,
        )
