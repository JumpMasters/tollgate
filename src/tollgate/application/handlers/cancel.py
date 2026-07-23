"""The cancel command handler: release a held reservation's full estimate.

The call failed before incurring usage, so the whole worst-case hold goes back: claim the
idempotency key, load and authorize the reservation, claim the identity guard
(``held → released``), release every line in canonical lock order, append the ``release``
ledger rows, cache the response — one transaction. Cancel has **no** self-heal path: a
reaped reservation was already released by the reaper, so a cancel that lost the guard is
rejected with :class:`ReservationNotHeld` (the release effect happened exactly once either
way). Denials raise and roll back; only a success persists its key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    command_fingerprint,
    load_owned_reservation,
    ordered_lines,
)
from tollgate.application.ports import IdGenerator, UnitOfWork
from tollgate.domain.commands import CancelCommand, CancelResult
from tollgate.domain.credentials import Principal
from tollgate.domain.errors import IdempotencyKeyReuse, ReservationNotHeld, TollgateError
from tollgate.domain.ids import ReservationId
from tollgate.domain.records import ClaimOutcome, LedgerEntry, LedgerKind
from tollgate.domain.reservations import ReservationStatus


def cancel_fingerprint(principal: Principal, command: CancelCommand) -> str:
    """Fingerprint of a cancel for idempotency-key reuse detection.

    The ``"command"`` discriminator keeps a cancel and a commit of the same reservation from
    ever colliding under one key.
    """
    return command_fingerprint(
        {
            "command": "cancel",
            "principal_id": principal.user_id,
            "reservation_id": command.reservation_id,
        }
    )


def _result_to_response(result: CancelResult) -> dict[str, Any]:
    """Serialize a cancel result to the JSON response cached under its idempotency key."""
    return {
        "reservation_id": result.reservation_id,
        "released_micro": result.released_micro,
    }


def _result_from_response(data: Mapping[str, Any]) -> CancelResult:
    """Reconstruct a cancel result from a cached idempotency response (the replay path)."""
    return CancelResult(
        reservation_id=ReservationId(str(data["reservation_id"])),
        released_micro=int(data["released_micro"]),
    )


class CancelHandler:
    """Runs the cancel command end-to-end inside one transaction."""

    def __init__(self, *, uow: UnitOfWork, ids: IdGenerator) -> None:
        self._uow = uow
        self._ids = ids

    async def cancel(self, auth: AuthContext, command: CancelCommand) -> CancelResult:
        """Release the reservation's full estimate on every line, exactly once.

        Raises :class:`ScopeNotAuthorized` for an unknown or foreign reservation (identically —
        no existence leak), :class:`ReservationNotHeld` when the reservation already settled
        (committed, released, or reaped), and :class:`IdempotencyKeyReuse` on key reuse.
        """
        fingerprint = cancel_fingerprint(auth.principal, command)
        principal_id = auth.credential.principal_id
        async with self._uow.begin() as tx:
            claim = await tx.idempotency.claim(principal_id, command.idempotency_key, fingerprint)
            if claim.outcome is ClaimOutcome.REPLAY:
                response = claim.response
                if response is None:  # pragma: no cover - a committed command always stored one
                    raise TollgateError("idempotency replay is missing its stored response")
                return _result_from_response(response)
            if claim.outcome is ClaimOutcome.MISMATCH:
                raise IdempotencyKeyReuse

            stored = await load_owned_reservation(tx.reservations, auth, command.reservation_id)
            lines = ordered_lines(await tx.reservations.find_lines(command.reservation_id))
            if not await tx.reservations.claim_terminal(
                command.reservation_id, ReservationStatus.RELEASED
            ):
                raise ReservationNotHeld

            entries: list[LedgerEntry] = []
            for line in lines:
                await tx.counter_store.release(
                    line.node.budget_id, line.period_start, line.amount_micro
                )
                entries.append(
                    LedgerEntry(
                        entry_id=self._ids.new_ledger_entry_id(),
                        kind=LedgerKind.RELEASE,
                        budget_id=line.node.budget_id,
                        period_start=line.period_start,
                        reservation_id=command.reservation_id,
                        delta_reserved_micro=-line.amount_micro,
                        provider=stored.record.provider,
                        price_book_version=stored.record.price_book_version,
                    )
                )
            await tx.ledger.append(entries)

            result = CancelResult(
                reservation_id=command.reservation_id,
                released_micro=stored.record.estimated_micro,
            )
            await tx.idempotency.store_response(
                principal_id,
                command.idempotency_key,
                _result_to_response(result),
            )
            return result
