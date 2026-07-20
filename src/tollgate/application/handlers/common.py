"""Shared helpers for the command handlers (§5).

The five mutating commands share four concerns: the canonical idempotency fingerprint (§5.1),
a reservation ownership check that never reveals whether a foreign reservation exists (§5.0),
the canonical §5.3 lock ordering of a reservation's lines, and applicable-set resolution with
the project authorization re-check (§4, §5.0). They live here so reserve (plan 09) and the
lifecycle/backfill commands (plan 10) cannot drift apart. Helpers take the narrowest port they
need, not the whole command context.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from tollgate.application.auth import AuthContext, require_scope
from tollgate.application.ports import BudgetRepository, ReservationRepository
from tollgate.domain.errors import ScopeNotAuthorized
from tollgate.domain.ids import ProjectId, ReservationId
from tollgate.domain.records import ReservationLineView, StoredReservation
from tollgate.domain.scopes import (
    BudgetNode,
    ScopeKind,
    lock_order_key,
    resolve_applicable_set,
)


def command_fingerprint(payload: dict[str, Any]) -> str:
    """Hash a command's salient fields into a stable idempotency fingerprint (§5.1).

    Canonical JSON (sorted keys — including nested mappings — no whitespace), then SHA-256, so
    field order never changes the fingerprint. Callers pass every field that distinguishes one
    logical command from another under the same key, including a ``"command"`` discriminator
    where two command kinds could otherwise collide.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def load_owned_reservation(
    reservations: ReservationRepository, auth: AuthContext, reservation_id: ReservationId
) -> StoredReservation:
    """Load a reservation the acting principal owns, or deny without an existence leak (§5.0).

    Only the principal a reservation was reserved for may drive its lifecycle — the same
    identity the credential derived at reserve time. An unknown id and another principal's
    reservation raise the same :class:`ScopeNotAuthorized`, so the response never reveals
    which reservations exist.
    """
    stored = await reservations.find(reservation_id)
    if stored is None or stored.record.principal_id != auth.credential.principal_id:
        raise ScopeNotAuthorized(f"reservation:{reservation_id}")
    return stored


def ordered_lines(lines: Sequence[ReservationLineView]) -> list[ReservationLineView]:
    """Sort a reservation's lines into the canonical §5.3 lock order.

    Terminal commands update the same ``budget_balance`` rows concurrent reserves contend on;
    walking them in the one shared order — scope rank, scope_id, period_start — is what keeps
    a commit/cancel from forming a lock cycle with a reserve on the shared parent rows.
    """
    return sorted(lines, key=lambda line: (*lock_order_key(line.node), line.period_start))


async def resolve_applicable_nodes(
    budgets: BudgetRepository, auth: AuthContext, project_id: ProjectId | None
) -> tuple[BudgetNode, ...]:
    """Resolve the applicable budget set: ancestry plus the authorized project (§4, §5.0, §5.3).

    The project's org ancestry is server-derived (never the request's assertion) and re-checked
    against the credential inside the transaction; an unknown project is rejected identically
    to an unauthorized one. Raises ``BudgetNotFound`` when the set is empty (default-deny) —
    via :func:`resolve_applicable_set`, which also returns the nodes in canonical lock order.
    """
    ancestry = await budgets.find_ancestry_budgets(auth.principal)
    project_node: BudgetNode | None = None
    if project_id is not None:
        resolved = await budgets.find_project(project_id)
        if resolved is None:
            raise ScopeNotAuthorized(f"project:{project_id}")
        require_scope(
            auth.credential,
            {ScopeKind.ORG: resolved.org_id, ScopeKind.PROJECT: project_id},
            target=f"project:{project_id}",
        )
        project_node = resolved.budget
    return resolve_applicable_set(ancestry, project_node)
