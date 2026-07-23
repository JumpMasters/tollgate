"""The commit command handler: reconcile a held reservation against actual usage.

Runs the command envelope in one transaction: claim the idempotency key, load and authorize the
reservation, recompute the actual cost from provider-reported tokens at the reservation's
**stamped** price-book version (never the latest), claim the identity guard, then walk the
lines in canonical lock order moving at most the reserved estimate into committed with any
excess as audited overage. A commit that finds the reservation *reaped* does not no-op: it
claims the one legal post-reap transition and applies the actual against each line's live
remaining — the self-healing late commit (ADR 0029) — so real, already-incurred spend is
always recorded. Every denial raises a typed error that rolls the whole transaction back; only
a success persists its idempotency key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    command_fingerprint,
    load_owned_reservation,
    ordered_lines,
)
from tollgate.application.ports import CommandContext, IdGenerator, UnitOfWork
from tollgate.domain.commands import CommitCommand, CommitResult
from tollgate.domain.credentials import Principal
from tollgate.domain.errors import (
    IdempotencyKeyReuse,
    ReservationNotHeld,
    TollgateError,
    UnknownModel,
)
from tollgate.domain.ids import ReservationId
from tollgate.domain.pricing import actual_micro, reconcile
from tollgate.domain.records import (
    ClaimOutcome,
    LedgerEntry,
    LedgerKind,
    ReservationLineView,
    ReservationRecord,
)
from tollgate.domain.reservations import ReservationStatus


def commit_fingerprint(principal: Principal, command: CommitCommand) -> str:
    """Fingerprint of a commit for idempotency-key reuse detection.

    Folds the derived principal, the target reservation, and the provider-reported usage — a
    retry of the same reconciliation matches; a different one is key reuse. The ``"command"``
    discriminator keeps a commit and a cancel of the same reservation from ever colliding.
    """
    return command_fingerprint(
        {
            "command": "commit",
            "principal_id": principal.user_id,
            "reservation_id": command.reservation_id,
            "input_tokens": command.usage.input_tokens,
            "output_tokens": command.usage.output_tokens,
            "cached_input_tokens": command.usage.cached_input_tokens,
            "cache_creation_tokens": command.usage.cache_creation_tokens,
        }
    )


def _result_to_response(result: CommitResult) -> dict[str, Any]:
    """Serialize a commit result to the JSON response cached under its idempotency key."""
    return {
        "reservation_id": result.reservation_id,
        "committed_micro": result.committed_micro,
        "overage_micro": result.overage_micro,
    }


def _result_from_response(data: Mapping[str, Any]) -> CommitResult:
    """Reconstruct a commit result from a cached idempotency response (the replay path)."""
    return CommitResult(
        reservation_id=ReservationId(str(data["reservation_id"])),
        committed_micro=int(data["committed_micro"]),
        overage_micro=int(data["overage_micro"]),
    )


class CommitHandler:
    """Runs the commit command end-to-end inside one transaction."""

    def __init__(self, *, uow: UnitOfWork, ids: IdGenerator) -> None:
        self._uow = uow
        self._ids = ids

    async def commit(self, auth: AuthContext, command: CommitCommand) -> CommitResult:
        """Reconcile the reservation against provider-reported usage, exactly once.

        Raises :class:`ScopeNotAuthorized` for an unknown or foreign reservation (identically —
        no existence leak), :class:`ReservationNotHeld` when the terminal effect already
        happened under a different key, :class:`IdempotencyKeyReuse` on key reuse, and
        :class:`UnknownModel` if the stamped price row is missing. A reaped reservation
        self-heals instead of failing.
        """
        fingerprint = commit_fingerprint(auth.principal, command)
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
            record = stored.record
            price = await tx.prices.price_at(
                record.price_book_version, record.provider, record.model
            )
            if price is None:
                raise UnknownModel(record.provider, record.model)
            actual = actual_micro(
                price,
                input_tokens=command.usage.input_tokens,
                output_tokens=command.usage.output_tokens,
                cached_input_tokens=command.usage.cached_input_tokens,
                cache_creation_tokens=command.usage.cache_creation_tokens,
            )
            lines = ordered_lines(await tx.reservations.find_lines(command.reservation_id))

            # The identity guards decide the path: held -> normal reconciliation;
            # reaped -> the one legal post-reap transition, the self-healing late commit
            # (ADR 0029); anything else already settled -> rejected.
            if await tx.reservations.claim_terminal(
                command.reservation_id, ReservationStatus.COMMITTED
            ):
                result = await self._commit_held(tx, command, lines, record, actual)
            elif await tx.reservations.claim_late_commit(command.reservation_id):
                result = await self._commit_reaped(tx, command, lines, record, actual)
            else:
                raise ReservationNotHeld
            await tx.idempotency.store_response(
                principal_id,
                command.idempotency_key,
                _result_to_response(result),
            )
            return result

    async def _commit_held(
        self,
        tx: CommandContext,
        command: CommitCommand,
        lines: Sequence[ReservationLineView],
        record: ReservationRecord,
        actual: int,
    ) -> CommitResult:
        """The normal path: move at most the reserved estimate on every line."""
        entries: list[LedgerEntry] = []
        for line in lines:
            # Every line held the same estimate in V1, so line_split == the result split;
            # computing per line keeps the ledger faithful should they ever diverge.
            line_split = reconcile(reserved_micro=line.amount_micro, actual=actual)
            await tx.counter_store.commit(
                line.node.budget_id,
                line.period_start,
                reserved_micro=line.amount_micro,
                actual_micro=actual,
            )
            entries.append(
                LedgerEntry(
                    entry_id=self._ids.new_ledger_entry_id(),
                    kind=LedgerKind.COMMIT_ADJUST,
                    budget_id=line.node.budget_id,
                    period_start=line.period_start,
                    reservation_id=command.reservation_id,
                    delta_reserved_micro=-line.amount_micro,
                    delta_committed_micro=line_split.committed_micro,
                    actual_input_tokens=command.usage.input_tokens,
                    actual_output_tokens=command.usage.output_tokens,
                    provider=record.provider,
                    price_book_version=record.price_book_version,
                )
            )
            if line_split.overage_micro > 0:
                entries.append(
                    LedgerEntry(
                        entry_id=self._ids.new_ledger_entry_id(),
                        kind=LedgerKind.OVERAGE,
                        budget_id=line.node.budget_id,
                        period_start=line.period_start,
                        reservation_id=command.reservation_id,
                        delta_overage_micro=line_split.overage_micro,
                        actual_input_tokens=command.usage.input_tokens,
                        actual_output_tokens=command.usage.output_tokens,
                        provider=record.provider,
                        price_book_version=record.price_book_version,
                    )
                )
        await tx.ledger.append(entries)
        split = reconcile(reserved_micro=record.estimated_micro, actual=actual)
        return CommitResult(
            reservation_id=command.reservation_id,
            committed_micro=split.committed_micro,
            overage_micro=split.overage_micro,
        )

    async def _commit_reaped(
        self,
        tx: CommandContext,
        command: CommitCommand,
        lines: Sequence[ReservationLineView],
        record: ReservationRecord,
        actual: int,
    ) -> CommitResult:
        """The self-heal: record real spend against each line's live remaining (ADR 0029).

        The reap already released the estimate, so nothing moves out of ``reserved``
        (``delta_reserved == 0``); committed takes what fits in each node's remaining and the
        excess is audited overage. The result reports the most-restrictive node's split.
        """
        worst_overage = 0
        entries: list[LedgerEntry] = []
        for line in lines:
            applied = await tx.counter_store.apply_spend(
                line.node.budget_id, line.period_start, actual
            )
            worst_overage = max(worst_overage, applied.overage_micro)
            entries.append(
                LedgerEntry(
                    entry_id=self._ids.new_ledger_entry_id(),
                    kind=LedgerKind.COMMIT_ADJUST,
                    budget_id=line.node.budget_id,
                    period_start=line.period_start,
                    reservation_id=command.reservation_id,
                    delta_committed_micro=applied.committed_micro,
                    actual_input_tokens=command.usage.input_tokens,
                    actual_output_tokens=command.usage.output_tokens,
                    provider=record.provider,
                    price_book_version=record.price_book_version,
                    ref="late_commit",
                )
            )
            if applied.overage_micro > 0:
                entries.append(
                    LedgerEntry(
                        entry_id=self._ids.new_ledger_entry_id(),
                        kind=LedgerKind.OVERAGE,
                        budget_id=line.node.budget_id,
                        period_start=line.period_start,
                        reservation_id=command.reservation_id,
                        delta_overage_micro=applied.overage_micro,
                        actual_input_tokens=command.usage.input_tokens,
                        actual_output_tokens=command.usage.output_tokens,
                        provider=record.provider,
                        price_book_version=record.price_book_version,
                        ref="late_commit",
                    )
                )
        await tx.ledger.append(entries)
        return CommitResult(
            reservation_id=command.reservation_id,
            committed_micro=actual - worst_overage,
            overage_micro=worst_overage,
        )
