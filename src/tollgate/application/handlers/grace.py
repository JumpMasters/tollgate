"""The grace backfill handler: reconcile spend from an enforcement outage (§5.6, ADR 0030).

Under opt-in grace the SDK dispatched calls while the datastore was unreachable, tracking the
provider-reported usage locally; once connectivity returns it backfills that spend so it is
never lost. There is no reservation: the handler resolves everything server-side at backfill
time — the applicable budget set (credential ancestry union authorized project, the same policy
and re-check as reserve), the current price-book version (ADR 0028), and the current UTC
calendar-month period (ADR 0027) — then applies the actual cost against each node's live
remaining (committed up to the remaining, excess as audited overage, ADR 0029's split) and
appends one ``grace_backfill`` ledger row per node carrying both deltas. An empty applicable
set is rejected: with no governing budget there is no balance to reconcile against. One §5
transaction; denials raise and roll back; only a success persists its key (§5.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    RESPONSE_SUCCEEDED,
    command_fingerprint,
    resolve_applicable_nodes,
)
from tollgate.application.ports import Clock, IdGenerator, UnitOfWork
from tollgate.domain.commands import GraceBackfillCommand, GraceBackfillResult
from tollgate.domain.credentials import Principal
from tollgate.domain.errors import IdempotencyKeyReuse, TollgateError, UnknownModel
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.pricing import actual_micro
from tollgate.domain.records import ClaimOutcome, LedgerEntry, LedgerKind


def grace_backfill_fingerprint(principal: Principal, command: GraceBackfillCommand) -> str:
    """Fingerprint of a grace backfill for idempotency-key reuse detection (§5.1).

    Folds the derived principal and every field that determines what the backfill records, so
    the SDK's retry of the same window matches while a different window is key reuse — the
    "backfills exactly once" guarantee rides on this key.
    """
    return command_fingerprint(
        {
            "command": "grace_backfill",
            "principal_id": principal.user_id,
            "provider": command.provider,
            "model": command.model,
            "input_tokens": command.usage.input_tokens,
            "output_tokens": command.usage.output_tokens,
            "cached_input_tokens": command.usage.cached_input_tokens,
            "cache_creation_tokens": command.usage.cache_creation_tokens,
            "project_id": command.project_id,
        }
    )


def _result_to_response(result: GraceBackfillResult) -> dict[str, Any]:
    """Serialize a backfill result to the JSON response cached under its idempotency key."""
    return {
        "actual_micro": result.actual_micro,
        "price_book_version": result.price_book_version,
    }


def _result_from_response(data: Mapping[str, Any]) -> GraceBackfillResult:
    """Reconstruct a backfill result from a cached idempotency response (the replay path)."""
    return GraceBackfillResult(
        actual_micro=int(data["actual_micro"]),
        price_book_version=str(data["price_book_version"]),
    )


class GraceBackfillHandler:
    """Runs the grace backfill end-to-end inside one transaction (§5.6)."""

    def __init__(self, *, uow: UnitOfWork, clock: Clock, ids: IdGenerator) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids

    async def backfill(
        self, auth: AuthContext, command: GraceBackfillCommand
    ) -> GraceBackfillResult:
        """Record grace-window spend against every applicable budget (§5.6, ADR 0030).

        Raises :class:`UnknownModel` when the pair is unpriced, :class:`ScopeNotAuthorized`
        for an unauthorized or unknown project (identically), ``BudgetNotFound`` when no
        budget governs the request, and :class:`IdempotencyKeyReuse` on key reuse — each
        rolling the transaction back for the SDK to surface: grace spend that cannot be
        attributed is an operational alert, not silent loss.
        """
        fingerprint = grace_backfill_fingerprint(auth.principal, command)
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
            for node in nodes:  # already in canonical lock order (resolve_applicable_set)
                await tx.counter_store.ensure_period(node.budget_id, period_start)
                applied = await tx.counter_store.apply_spend(node.budget_id, period_start, actual)
                entries.append(
                    LedgerEntry(
                        entry_id=self._ids.new_ledger_entry_id(),
                        kind=LedgerKind.GRACE_BACKFILL,
                        budget_id=node.budget_id,
                        period_start=period_start,
                        delta_committed_micro=applied.committed_micro,
                        delta_overage_micro=applied.overage_micro,
                        actual_input_tokens=command.usage.input_tokens,
                        actual_output_tokens=command.usage.output_tokens,
                        provider=command.provider,
                        price_book_version=priced.version,
                    )
                )
            await tx.ledger.append(entries)

            result = GraceBackfillResult(actual_micro=actual, price_book_version=priced.version)
            await tx.idempotency.store_response(
                principal_id,
                command.idempotency_key,
                RESPONSE_SUCCEEDED,
                _result_to_response(result),
            )
            return result
