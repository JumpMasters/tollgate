"""The reserve command handler and its transaction envelope (§4, §5).

``ReserveHandler.reserve`` is the keystone command. Authentication is the caller's precondition —
the handler receives an already-verified :class:`AuthContext` (plan 11's bearer dependency) — and
then runs the §5 envelope in one transaction via the :class:`UnitOfWork` port: claim the
idempotency key, resolve the current price and the applicable budget set, guarded-reserve the
worst-case estimate on every node all-or-nothing, persist the held reservation, its lines and the
reserve ledger rows, and cache the response. A denial (unknown model, unauthorized/unknown project,
empty applicable set, insufficient budget) raises a typed error, which leaves the ``UnitOfWork`` via
an exception and rolls the whole transaction back — so a budget denial never persists its
idempotency key and a later retry can still succeed (§5.1). Resolution and the project authorization
re-check happen inside the transaction (§5.0 re-checks authorization inside resolution).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from tollgate.application.auth import AuthContext
from tollgate.application.handlers.common import (
    RESPONSE_SUCCEEDED,
    command_fingerprint,
    resolve_applicable_nodes,
)
from tollgate.application.ports import Clock, IdGenerator, UnitOfWork
from tollgate.domain.commands import ReserveCommand, ReserveResult
from tollgate.domain.credentials import Principal
from tollgate.domain.errors import (
    IdempotencyKeyReuse,
    InsufficientBudget,
    NonPositiveEstimate,
    TollgateError,
    UnknownModel,
)
from tollgate.domain.ids import ReservationId
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.pricing import estimate_micro
from tollgate.domain.records import (
    ClaimOutcome,
    LedgerEntry,
    LedgerKind,
    ReservationLineRecord,
    ReservationRecord,
)
from tollgate.domain.scopes import BudgetNode


def reserve_fingerprint(principal: Principal, command: ReserveCommand) -> str:
    """Return a stable fingerprint of a reserve command for idempotency-key reuse detection (§5.1).

    Two reserves under the same idempotency key but different salient fields are key reuse and
    are rejected; the fingerprint folds the derived principal and every cost-determining field
    so a retry of the *same* logical call matches while a different call does not. The
    canonical JSON sorts keys (labels included), so field order never changes the fingerprint.
    """
    return command_fingerprint(
        {
            "principal_id": principal.user_id,
            "provider": command.provider,
            "model": command.model,
            "input_bound_tokens": command.input_bound_tokens,
            "max_output_tokens": command.max_output_tokens,
            "project_id": command.project_id,
            "labels": dict(command.labels),
        }
    )


def _binding_label(node: BudgetNode | None) -> str:
    """Human-readable name of the node that denied a reserve (most-restrictive resolution, §5.3)."""
    if node is None:  # pragma: no cover - a denied reserve always names its binding node (plan 07)
        return "unknown"
    return f"{node.scope_kind}:{node.scope_id}"


def _result_to_response(result: ReserveResult) -> dict[str, Any]:
    """Serialize a reserve result to the JSON response cached under its idempotency key."""
    return {
        "reservation_id": result.reservation_id,
        "estimated_micro": result.estimated_micro,
        "price_book_version": result.price_book_version,
        "ttl_deadline": result.ttl_deadline.isoformat(),
    }


def _result_from_response(data: Mapping[str, Any]) -> ReserveResult:
    """Reconstruct a reserve result from a cached idempotency response (the replay path, §5.1)."""
    return ReserveResult(
        reservation_id=ReservationId(str(data["reservation_id"])),
        estimated_micro=int(data["estimated_micro"]),
        price_book_version=str(data["price_book_version"]),
        ttl_deadline=datetime.fromisoformat(str(data["ttl_deadline"])),
    )


class ReserveHandler:
    """Runs the reserve command end-to-end inside one transaction (§4, §5)."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        reservation_ttl_seconds: int,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._ttl_seconds = reservation_ttl_seconds

    async def reserve(self, auth: AuthContext, command: ReserveCommand) -> ReserveResult:
        """Reserve the worst-case estimate against every applicable budget, all-or-nothing (§4, §5).

        ``auth`` is the already-verified principal (authentication is the caller's precondition).
        Raises a typed denial — :class:`UnknownModel`, :class:`ScopeNotAuthorized`,
        ``BudgetNotFound`` (empty set), or :class:`InsufficientBudget` — which rolls the whole
        transaction back; :class:`IdempotencyKeyReuse` on a key reused with a different command.
        """
        fingerprint = reserve_fingerprint(auth.principal, command)
        principal_id = auth.credential.principal_id
        async with self._uow.begin() as tx:
            claim = await tx.idempotency.claim(principal_id, command.idempotency_key, fingerprint)
            if claim.outcome is ClaimOutcome.REPLAY:
                response = claim.response
                if response is None:  # pragma: no cover - a committed reserve always stored one
                    raise TollgateError("idempotency replay is missing its stored response")
                return _result_from_response(response)
            if claim.outcome is ClaimOutcome.MISMATCH:
                raise IdempotencyKeyReuse

            priced = await tx.prices.resolve_price(command.provider, command.model)
            if priced is None:
                raise UnknownModel(command.provider, command.model)

            nodes = await resolve_applicable_nodes(tx.budgets, auth, command.project_id)

            estimate = estimate_micro(
                priced.price,
                input_bound_tokens=command.input_bound_tokens,
                max_output_tokens=command.max_output_tokens,
            )
            if estimate == 0:
                raise NonPositiveEstimate(
                    "worst-case estimate is zero; a reserve must gate a positive amount"
                )
            now = self._clock.now()
            period_start = calendar_month_start(now)
            ttl_deadline = now + timedelta(seconds=self._ttl_seconds)
            reservation_id = self._ids.new_reservation_id()

            outcome = await tx.reserve_tx.reserve(nodes, period_start, estimate)
            if not outcome.ok:
                raise InsufficientBudget(_binding_label(outcome.binding_node))

            record = ReservationRecord(
                reservation_id=reservation_id,
                idempotency_key=command.idempotency_key,
                principal_id=auth.credential.principal_id,
                provider=command.provider,
                model=command.model,
                price_book_version=priced.version,
                estimated_micro=estimate,
                input_bound_tokens=command.input_bound_tokens,
                max_output_tokens=command.max_output_tokens,
                ttl_deadline=ttl_deadline,
                labels=command.labels,
            )
            lines = [
                ReservationLineRecord(
                    reservation_id=reservation_id,
                    budget_id=node.budget_id,
                    period_start=period_start,
                    amount_micro=estimate,
                )
                for node in nodes
            ]
            entries = [
                LedgerEntry(
                    entry_id=self._ids.new_ledger_entry_id(),
                    kind=LedgerKind.RESERVE,
                    budget_id=node.budget_id,
                    period_start=period_start,
                    reservation_id=reservation_id,
                    delta_reserved_micro=estimate,
                    provider=command.provider,
                    price_book_version=priced.version,
                )
                for node in nodes
            ]
            await tx.reservations.insert(record, lines)
            await tx.ledger.append(entries)

            result = ReserveResult(
                reservation_id=reservation_id,
                estimated_micro=estimate,
                price_book_version=priced.version,
                ttl_deadline=ttl_deadline,
            )
            await tx.idempotency.store_response(
                principal_id,
                command.idempotency_key,
                RESPONSE_SUCCEEDED,
                _result_to_response(result),
            )
            return result
