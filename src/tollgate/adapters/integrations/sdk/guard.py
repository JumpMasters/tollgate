"""The guard: reserve worst-case budget before dispatch, commit/cancel in a finally (section 4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from tollgate.adapters.integrations.sdk.client import ProviderUsage, TollgateClient
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import InvalidRequest
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
        """Record the provider-reported usage to reconcile on commit (section 4)."""
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
    """Extend the reservation's TTL every ``interval`` seconds until cancelled (section 5.4)."""
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
    """Reserve before dispatch; commit on clean exit with usage, else cancel (section 4,
    section 5.4).

    A denied reserve (``BudgetDenied``/``NotAuthorized``) or an unreachable control plane
    (``EnforcementUnavailable``, fail-closed) raises *before* the body runs, so the model call
    never dispatches. On exit the reservation is always resolved: commit the recorded usage on a
    clean exit, otherwise cancel (the body raised, or the caller recorded no usage).

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
        with contextlib.suppress(Exception):
            await client.cancel(reservation_id=call.reservation_id, idempotency_key=new_key())
        raise
    else:
        await _stop(heartbeat)
        if call.usage is not None:
            await client.commit(
                reservation_id=call.reservation_id, usage=call.usage, idempotency_key=new_key()
            )
        else:
            await client.cancel(reservation_id=call.reservation_id, idempotency_key=new_key())
