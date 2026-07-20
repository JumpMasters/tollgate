"""The shared background-worker polling loop (§5.5).

A worker is a pure ``run_once`` tick (a reaper) wrapped by ``run_forever``, which polls it on a
fixed interval until a stop event is set. Each tick is bounded and idempotent, so a *transient*
failure is logged and the loop continues — a reaper is a backstop and must not die on one bad
poll. But a *persistent* failure (a wrong ``TOLLGATE_DATABASE_URL``, revoked grants, unapplied
migrations) would otherwise fail every tick forever while looking healthy to the orchestrator, so
consecutive failures back off exponentially and, past a threshold, the loop exits non-zero for the
orchestrator to restart and alert (#75). This module imports only the standard library: the tick
logic lives in the application layer and the concrete wiring in ``app.py`` (``app → workers``), so
``workers`` never imports ``adapters``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

#: Cap the backoff exponent before it is applied. ``worker_max_consecutive_failures`` only enforces
#: ``ge=1`` (no upper bound), so an operator can set it high enough that ``2 ** (n - 1)`` overflows
#: when multiplied by the float base — before the ``min`` with the ceiling ever runs. 2**30 s is
#: already ~34 years, far above any sane ceiling, so clamping the exponent here is lossless (#107).
_MAX_BACKOFF_EXPONENT = 30


class WorkerStalled(RuntimeError):
    """A worker made no forward progress for too many consecutive ticks (#91).

    Distinct from a tick that *raised*: the ticks returned normally but reported failures without
    reaping anything, so the loop escalates by raising this for the orchestrator to restart/alert.
    """


class SupportsRunOnce(Protocol):
    """One bounded, idempotent unit of polled work (a reaper's ``run_once``)."""

    async def run_once(self) -> object: ...


def _backoff_seconds(consecutive_failures: int, *, base: float, cap: float) -> float:
    """Exponential backoff for the *n*-th consecutive failure, clamped to ``cap`` (#75, #107)."""
    exponent = min(consecutive_failures - 1, _MAX_BACKOFF_EXPONENT)
    return min(base * 2.0**exponent, cap)


def _reported_failure_without_progress(result: object) -> bool:
    """Whether a structured tick result reaped nothing while hitting per-item failures (#91).

    Duck-typed so the loop stays generic and stdlib-only: a reservation-reaper ``ReapReport``
    exposes ``reaped``/``failed``, whereas the idempotency reaper returns a plain ``int`` (no such
    fields) and is therefore never judged stalled.
    """
    failed = getattr(result, "failed", 0)
    reaped = getattr(result, "reaped", 0)
    return isinstance(failed, int) and isinstance(reaped, int) and failed > 0 and reaped == 0


async def run_forever(
    tick: SupportsRunOnce,
    *,
    interval_seconds: float,
    stop: asyncio.Event,
    name: str,
    max_consecutive_failures: int = 10,
    backoff_base_seconds: float = 1.0,
    backoff_max_seconds: float = 60.0,
) -> None:
    """Poll ``tick.run_once`` every ``interval_seconds`` until ``stop`` is set (§5.5).

    Between ticks the loop waits the interval but wakes immediately when ``stop`` is set (graceful
    shutdown). A tick counts as a failure when it *raises* or when it returns a structured result
    that reaped nothing while reporting per-item failures — the latter is the reaper stuck behind
    poison rows, which a normal return would otherwise hide from the escalation (#91). A failed
    tick is logged and the loop continues after an exponential backoff (``backoff_base_seconds``
    doubling up to ``backoff_max_seconds``); any forward progress resets the counter. After
    ``max_consecutive_failures`` in a row the loop exits non-zero (re-raising a raised tick, or
    raising :class:`WorkerStalled`), so the orchestrator restarts and alerts rather than a wedged
    worker looking healthy (#75).
    """
    consecutive_failures = 0
    while not stop.is_set():
        try:
            result = await tick.run_once()
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "%s tick failed (%d in a row); backing off", name, consecutive_failures
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "%s failed %d ticks in a row; exiting for the orchestrator to restart",
                    name,
                    consecutive_failures,
                )
                raise
            wait = _backoff_seconds(
                consecutive_failures, base=backoff_base_seconds, cap=backoff_max_seconds
            )
        else:
            logger.info("%s tick complete: %r", name, result)
            if _reported_failure_without_progress(result):
                consecutive_failures += 1
                logger.error(
                    "%s reaped nothing but reported failures (%d in a row); backing off",
                    name,
                    consecutive_failures,
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        "%s made no progress for %d ticks in a row; exiting for the orchestrator "
                        "to restart",
                        name,
                        consecutive_failures,
                    )
                    raise WorkerStalled(
                        f"{name} made no reaping progress for {consecutive_failures} ticks"
                    )
                wait = _backoff_seconds(
                    consecutive_failures, base=backoff_base_seconds, cap=backoff_max_seconds
                )
            else:
                consecutive_failures = 0
                wait = interval_seconds
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)
