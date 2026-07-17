"""Ports: the interfaces the application depends on, expressed as Protocols.

Concrete adapters implement these. The application is written against the protocols alone, so
the Postgres store (and, later, a Redis fast-path) can be swapped without touching handler
logic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from tollgate.domain.chargeback import BudgetState
from tollgate.domain.credentials import Credential, Principal
from tollgate.domain.ids import (
    BudgetId,
    LedgerEntryId,
    PrincipalId,
    ProjectId,
    ReservationId,
)
from tollgate.domain.pricing import ModelPrice, PricedModel, Reconciliation
from tollgate.domain.records import (
    IdempotencyClaim,
    LedgerEntry,
    ReservationLineRecord,
    ReservationLineView,
    ReservationRecord,
    StoredReservation,
)
from tollgate.domain.reservations import ReservationStatus
from tollgate.domain.scopes import BudgetNode, ReserveOutcome, ResolvedProject, ScopeKind


class CounterStore(Protocol):
    """The budget-balance primitives behind a reservation.

    Implementations enforce the spend invariant with guarded conditional writes: a reserve
    that would breach a limit must fail rather than overshoot.
    """

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        """Lazily create the period's balance row, seeded from the budget's limit.

        Idempotent (``INSERT … ON CONFLICT DO NOTHING``) so concurrent first-reservers in a
        new period converge on one row rather than failing (§5.3, §5.5).
        """
        ...

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        """Reserve ``amount_micro`` against a budget node; return whether it fit."""
        ...

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        """Move a reservation's estimate to committed, recording any overage."""
        ...

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        """Release a held reservation's estimate back to the node."""
        ...

    async def apply_spend(
        self, budget_id: BudgetId, period_start: datetime, amount_micro: int
    ) -> Reconciliation:
        """Apply already-incurred spend against the node's live remaining (§5.4, §5.6).

        For the recovery paths with no held estimate — the self-healing late commit (ADR 0029)
        and the grace backfill (ADR 0030) — committed takes what fits in ``remaining = limit -
        reserved - committed - overage`` and the excess is recorded as audited overage. Returns
        the split so the caller can append faithful ledger rows; must never deny — real spend
        is always recorded.
        """
        ...


class ReservationRepository(Protocol):
    """Persistence for reservation rows, their lines, and the identity guard."""

    async def insert(
        self,
        reservation: ReservationRecord,
        lines: Sequence[ReservationLineRecord],
    ) -> None:
        """Persist a held reservation and its per-node lines in the current transaction."""
        ...

    async def claim_terminal(
        self, reservation_id: ReservationId, next_status: ReservationStatus
    ) -> bool:
        """Atomically move a reservation from held to a terminal state.

        Returns whether this caller won the claim, which is what makes a terminal effect
        exactly-once (§5.2). A second claim for the same reservation finds ``status ≠ 'held'``,
        matches zero rows, and returns ``False`` → idempotent replay / self-heal (§5.4).
        """
        ...

    async def find(self, reservation_id: ReservationId) -> StoredReservation | None:
        """Return the reservation row and its live status, or ``None`` if the id is unknown."""
        ...

    async def find_lines(self, reservation_id: ReservationId) -> Sequence[ReservationLineView]:
        """Return the reservation's lines joined with their budget nodes.

        The node lets terminal commands walk the balances in the canonical §5.3 lock order;
        each line's own ``period_start`` is carried because a late commit replays against the
        line's original period (ADR 0029).
        """
        ...

    async def claim_late_commit(self, reservation_id: ReservationId) -> bool:
        """Atomically move a reaped reservation to committed — the §5.4 self-heal guard.

        Returns whether this caller won; ``False`` means the reservation was not ``reaped``
        (still held, terminal, or already late-committed). The identity-guard mechanism of
        :meth:`claim_terminal`, applied to the one legal post-reap transition (ADR 0029).
        """
        ...

    async def advance_ttl(
        self, reservation_id: ReservationId, ttl_deadline: datetime
    ) -> datetime | None:
        """Monotonically advance a held reservation's TTL; return the resulting deadline.

        The stored deadline only ever moves forward, so a stale heartbeat can never shorten a
        newer one (§5.4). Returns ``None`` when the reservation is not held.
        """
        ...


class IdempotencyRepository(Protocol):
    """Claim/replay store for command idempotency keys (§5.1)."""

    async def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        """Claim ``key`` for ``fingerprint``.

        ``FRESH`` if newly inserted (the caller owns the effect); ``REPLAY`` with the stored
        response if the key already completed; ``MISMATCH`` if the key exists under a different
        command fingerprint (key reuse).
        """
        ...

    async def store_response(self, key: str, status: str, response: Mapping[str, Any]) -> None:
        """Cache a command's response on its key row so a later duplicate replays it."""
        ...


class LedgerRepository(Protocol):
    """Append-only writer for the audit ledger (§5.2)."""

    async def append(self, entries: Sequence[LedgerEntry]) -> None:
        """Append one or more ledger rows in the current transaction (never summed here)."""
        ...


class ReserveTransaction(Protocol):
    """Multi-budget, all-or-nothing guarded reserve across an applicable set (§5.2/§5.3)."""

    async def reserve(
        self,
        nodes: Sequence[BudgetNode],
        period_start: datetime,
        amount_micro: int,
    ) -> ReserveOutcome:
        """Reserve ``amount_micro`` on every applicable node in lock order, all-or-nothing.

        Returns ``ReserveOutcome(ok=True)`` iff every node had headroom. On the first node
        without headroom returns ``ReserveOutcome(ok=False, binding_node=node)`` and leaves the
        walk's earlier reserves in place for the caller's transaction to roll back (§5.3).
        """
        ...


class CredentialRepository(Protocol):
    """Read-only lookups behind credential authentication (§5.0).

    Authentication runs *before* the command transaction: it hashes the presented token, finds
    the matching credential, and derives the principal. The repository returns rows faithfully —
    a *revoked* credential is still returned — so the authenticator, not the store, enforces the
    active-only rule and the store stays a thin, testable lookup.
    """

    async def find_by_token_hash(self, token_hash: str) -> Credential | None:
        """Return the credential whose ``token_hash`` matches, or ``None`` if none does."""
        ...

    async def load_principal(self, principal_id: PrincipalId) -> Principal | None:
        """Resolve a principal to its ``user -> team -> org`` identity, or ``None`` if absent."""
        ...


class Clock(Protocol):
    """A source of the current time, injected so handlers stay deterministic under test."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC ``datetime``."""
        ...


class IdGenerator(Protocol):
    """Mints the time-ordered (uuidv7) ids the application stamps on new rows."""

    def new_reservation_id(self) -> ReservationId:
        """Return a fresh reservation id."""
        ...

    def new_ledger_entry_id(self) -> LedgerEntryId:
        """Return a fresh ledger entry id."""
        ...


class PriceBookRepository(Protocol):
    """Resolves the current price for a ``(provider, model)`` from the versioned price book (§3)."""

    async def resolve_price(self, provider: str, model: str) -> PricedModel | None:
        """Return the current price and its version, or ``None`` if the pair is unpriced.

        "Current" is the price-book version with the latest ``published_at`` (ADR 0028); the
        returned version is stamped on the reservation so the matching commit reconciles against
        the same immutable basis.
        """
        ...

    async def price_at(self, version: str, provider: str, model: str) -> ModelPrice | None:
        """Return the price stamped at exactly ``version``, or ``None`` if that row is absent.

        A commit reconciles against the reservation's stamped version (§4), never the latest —
        the immutable price book guarantees the row still says what it said at reserve time.
        """
        ...


class BudgetRepository(Protocol):
    """Reads the budget nodes a reserve gates against (§4, §5.3)."""

    async def find_ancestry_budgets(self, principal: Principal) -> Sequence[BudgetNode]:
        """Return the budgets that exist on the principal's ``org`` / ``team`` / ``user`` nodes.

        Ancestry scopes without a budget are simply absent from the result; the applicable-set
        policy (``resolve_applicable_set``) skips them.
        """
        ...

    async def find_project(self, project_id: ProjectId) -> ResolvedProject | None:
        """Resolve a named project to its org and optional budget node, or ``None`` if unknown.

        ``None`` means no such project; the handler treats that as unauthorized rather than
        revealing the project's (non-)existence.
        """
        ...


class CommandContext(Protocol):
    """The repository Ports bound to one command's transaction (§5).

    A :class:`UnitOfWork` yields this inside an open transaction; every Port here shares the same
    connection, so a command's resolution reads and its guarded writes commit — or roll back — as
    one unit.
    """

    @property
    def prices(self) -> PriceBookRepository: ...
    @property
    def budgets(self) -> BudgetRepository: ...
    @property
    def idempotency(self) -> IdempotencyRepository: ...
    @property
    def reservations(self) -> ReservationRepository: ...
    @property
    def ledger(self) -> LedgerRepository: ...
    @property
    def reserve_tx(self) -> ReserveTransaction: ...
    @property
    def counter_store(self) -> CounterStore: ...


class UnitOfWork(Protocol):
    """Brackets a command in one database transaction — the §5 envelope.

    ``begin()`` opens a transaction and yields a :class:`CommandContext`; leaving the context
    normally commits, and leaving it via an exception rolls back — so an insufficient-budget denial
    (which raises) discards its partial reserves and its idempotency claim atomically (§5.1, §5.3).
    """

    def begin(self) -> AbstractAsyncContextManager[CommandContext]:
        """Open the command transaction and yield its bound repositories."""
        ...


class ChargebackRepository(Protocol):
    """Read-only budget-state queries for the chargeback API (section 2, 5.0). Off the command
    path.
    """

    async def subtree_states(
        self, scope_kind: ScopeKind, scope_id: str, period_start: datetime
    ) -> Sequence[BudgetState]:
        """Return the state of every budget node at or below ``(scope_kind, scope_id)`` for the
        period.

        LEFT-joins the balance for ``period_start``; a node with no activity yet has no balance
        row and is reported as zero state against the budget's ``hard_limit_micro``. Never seeds
        a row.
        """
        ...

    async def resolve_scope_ancestry(
        self, scope_kind: ScopeKind, scope_id: str
    ) -> Mapping[ScopeKind, str] | None:
        """Return a scope node's server-derived ancestry map, or ``None`` if the node does not
        exist.

        The map is what :func:`tollgate.domain.credentials.authorizes` consumes to check that a
        filter node is at or below the credential (section 5.0) -- built from trusted structure
        rows, never from request-asserted ids.
        """
        ...


class ChargebackReader(Protocol):
    """Read seam: a connection-bound :class:`ChargebackRepository` (mirrors ``UnitOfWork``)."""

    def begin(self) -> AbstractAsyncContextManager[ChargebackRepository]:
        """Open a read-only connection; the async context yields a repository bound to it."""
        ...
