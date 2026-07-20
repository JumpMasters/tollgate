"""The metering command handler: record already-incurred spend, never deny (section 6, ADR 0037).

Metering is post-charge chargeback: the call already happened, so there is no reservation and no
admission gate. Like the grace backfill (ADR 0030) the handler resolves everything server-side —
the applicable budget set (credential ancestry union authorized project), the current price-book
version (ADR 0028), and the current UTC calendar-month period (ADR 0027) — then applies the actual
cost against each node's live remaining (committed up to remaining, excess as audited overage,
ADR 0029's split) and appends one self-describing ``meter`` ledger row per node carrying the
provider, model, and chargeback labels. An empty applicable set is rejected. One transaction;
denials raise and roll back; only a success persists its receipt (section 5.1). Because a meter
applies spend with no reservation to guard it, that dedup lives in the never-reaped
``metered_receipt`` table rather than the TTL'd ``idempotency_key``, so a retry stays exactly-once
beyond any window instead of double-applying the spend once the key ages out (#92).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    command_fingerprint,
    resolve_applicable_nodes,
)
from tollgate.application.ports import Clock, IdGenerator, UnitOfWork
from tollgate.domain.commands import MeterCommand, MeterResult
from tollgate.domain.credentials import Principal
from tollgate.domain.errors import IdempotencyKeyReuse, TollgateError, UnknownModel
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.pricing import actual_micro
from tollgate.domain.records import ClaimOutcome, LedgerEntry, LedgerKind


def meter_fingerprint(principal: Principal, command: MeterCommand) -> str:
    """Fingerprint of a meter for idempotency-key reuse detection (section 5.1).

    Folds the derived principal and every field that determines what the meter records, so a
    retry of the same completed call matches while a different call is key reuse. ``labels`` are
    copied to a plain dict (canonical JSON sorts keys, so label order never changes the hash).
    """
    return command_fingerprint(
        {
            "command": "meter",
            "principal_id": principal.user_id,
            "provider": command.provider,
            "model": command.model,
            "input_tokens": command.usage.input_tokens,
            "output_tokens": command.usage.output_tokens,
            "cached_input_tokens": command.usage.cached_input_tokens,
            "cache_creation_tokens": command.usage.cache_creation_tokens,
            "project_id": command.project_id,
            "labels": dict(command.labels),
            "truncated": command.truncated,
        }
    )


def _result_to_response(result: MeterResult) -> dict[str, Any]:
    return {"actual_micro": result.actual_micro, "price_book_version": result.price_book_version}


def _result_from_response(data: Mapping[str, Any]) -> MeterResult:
    return MeterResult(
        actual_micro=int(data["actual_micro"]),
        price_book_version=str(data["price_book_version"]),
    )


class MeterHandler:
    """Runs the metering command end-to-end inside one transaction (section 6)."""

    def __init__(self, *, uow: UnitOfWork, clock: Clock, ids: IdGenerator) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids

    async def meter(self, auth: AuthContext, command: MeterCommand) -> MeterResult:
        """Record metered spend against every applicable budget; never deny (section 6, ADR 0037).

        Raises :class:`UnknownModel` when unpriced, :class:`ScopeNotAuthorized` for an
        unauthorized/unknown project (identically), ``BudgetNotFound`` when no budget governs the
        request, and :class:`IdempotencyKeyReuse` on key reuse — each rolls the transaction back.
        """
        fingerprint = meter_fingerprint(auth.principal, command)
        principal_id = auth.credential.principal_id
        ref = "truncated" if command.truncated else None
        async with self._uow.begin() as tx:
            claim = await tx.metered_receipt.claim(
                principal_id, command.idempotency_key, fingerprint
            )
            if claim.outcome is ClaimOutcome.REPLAY:
                response = claim.response
                if response is None:  # pragma: no cover - a committed command always stored one
                    raise TollgateError("idempotency replay is missing its stored response")
                return _result_from_response(response)
            if claim.outcome is ClaimOutcome.MISMATCH:
                raise IdempotencyKeyReuse

            priced = await tx.prices.resolve_price(command.provider, command.model)
            if priced is None:
                raise UnknownModel(command.provider, command.model)
            nodes = await resolve_applicable_nodes(tx.budgets, auth, command.project_id)
            actual = actual_micro(
                priced.price,
                input_tokens=command.usage.input_tokens,
                output_tokens=command.usage.output_tokens,
                cached_input_tokens=command.usage.cached_input_tokens,
                cache_creation_tokens=command.usage.cache_creation_tokens,
            )
            period_start = calendar_month_start(self._clock.now())

            entries: list[LedgerEntry] = []
            for node in nodes:  # canonical lock order (resolve_applicable_set)
                await tx.counter_store.ensure_period(node.budget_id, period_start)
                applied = await tx.counter_store.apply_spend(node.budget_id, period_start, actual)
                entries.append(
                    LedgerEntry(
                        entry_id=self._ids.new_ledger_entry_id(),
                        kind=LedgerKind.METER,
                        budget_id=node.budget_id,
                        period_start=period_start,
                        delta_committed_micro=applied.committed_micro,
                        delta_overage_micro=applied.overage_micro,
                        actual_input_tokens=command.usage.input_tokens,
                        actual_output_tokens=command.usage.output_tokens,
                        provider=command.provider,
                        price_book_version=priced.version,
                        ref=ref,
                        model=command.model,
                        labels=command.labels,
                    )
                )
            await tx.ledger.append(entries)

            result = MeterResult(actual_micro=actual, price_book_version=priced.version)
            await tx.metered_receipt.store_response(
                principal_id,
                command.idempotency_key,
                _result_to_response(result),
            )
            return result
