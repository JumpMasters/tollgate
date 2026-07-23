"""A model-based Hypothesis machine drives the real handlers over random valid
command sequences and asserts the spend invariants at every node after every step.

Each rule issues a real command against a testcontainer Postgres while a pure in-memory reference
model — built from the same domain functions — tracks the expected balances. After every step the
machine asserts the DB balance equals the model and upholds the per-node invariants; at teardown it
runs the offline oracle over the real ledger as an independent conservation/exactly-once check. It
is sequential by construction (concurrency is covered by the targeted race tests). Hypothesis rules
are synchronous, so the machine drives the async handlers through a private event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, multiple, rule
from loadtest.oracle import ALL_CHECKS, Check, OracleReport, load_and_check
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.schema import (
    budget,
    budget_balance,
    org,
    price,
    price_book,
    team,
    user_principal,
)
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.application.auth import AuthContext
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.reap import ReservationReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.domain.commands import (
    CancelCommand,
    CommitCommand,
    ExtendCommand,
    ProviderUsage,
    ReserveCommand,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import InsufficientBudget, ReservationNotHeld
from tollgate.domain.ids import (
    CredentialId,
    OrgId,
    PrincipalId,
    ReservationId,
    TeamId,
    UserId,
)
from tollgate.domain.invariants import Balance, node_invariants_hold
from tollgate.domain.periods import calendar_month_start
from tollgate.domain.pricing import ModelPrice, actual_micro, estimate_micro
from tollgate.domain.scopes import ScopeKind

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

# --- module globals set by the sync entrypoint; the machine drives everything through them ---
_ENGINE: AsyncEngine | None = None
_LOOP: asyncio.AbstractEventLoop | None = None


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    assert _LOOP is not None, "the stateful entrypoint must set the private event loop"
    return _LOOP.run_until_complete(coro)


# --- the fixed budget tree, seeded fresh per Hypothesis example ---
_START = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_PERIOD = calendar_month_start(_START)
_TTL = 600
_PRICE = ModelPrice(
    provider="anthropic",
    model="claude",
    input_micro_per_token=Decimal("1"),
    output_micro_per_token=Decimal("2"),
    cached_input_micro_per_token=Decimal("0.5"),
    cache_creation_micro_per_token=Decimal("1.25"),
)

# budget_id -> (scope_kind, scope_id, hard_limit_micro). Limits chosen so user nodes deny often
# and the shared team/org nodes take real contention.
_BUDGETS: dict[str, tuple[str, str, int]] = {
    "b-org": ("org", "o1", 100_000),
    "b-t1": ("team", "t1", 5_000),
    "b-t2": ("team", "t2", 5_000),
    "b-u1": ("user", "u1", 1_000),
    "b-u2": ("user", "u2", 1_500),
    "b-u3": ("user", "u3", 800),
    "b-u4": ("user", "u4", 2_000),
}
_USER_TEAM: dict[str, str] = {"u1": "t1", "u2": "t1", "u3": "t2", "u4": "t2"}
_USERS: list[str] = ["u1", "u2", "u3", "u4"]
_USER_BUDGET: dict[str, str] = {"u1": "b-u1", "u2": "b-u2", "u3": "b-u3", "u4": "b-u4"}
_TEAM_BUDGET: dict[str, str] = {"t1": "b-t1", "t2": "b-t2"}
# the applicable node set a reserve for each user gates against (org, its team, itself)
_APPLICABLE: dict[str, list[str]] = {
    user: ["b-org", _TEAM_BUDGET[_USER_TEAM[user]], _USER_BUDGET[user]] for user in _USERS
}
_DATA_TABLES = (
    "org, team, user_principal, project, api_credential, price_book, price, "
    "budget, budget_alert, budget_balance, reservation, reservation_line, "
    "ledger, idempotency_key, metered_receipt"
)


class _MutableClock:
    """A clock the machine advances to drive TTL/extend/reap deterministically (§5.4)."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: int) -> None:
        self._t += timedelta(seconds=seconds)


@dataclass
class _NodeBalance:
    limit: int
    reserved: int = 0
    committed: int = 0
    overage: int = 0


@dataclass
class _ResState:
    nodes: list[str]
    estimate: int
    ttl: datetime
    owner: str
    status: str = "held"  # held | committed | released | reaped


class _ReferenceModel:
    """Pure mirror of the budget balances the handlers should produce (the test's source of
    truth)."""

    def __init__(self, limits: dict[str, int]) -> None:
        self.nodes: dict[str, _NodeBalance] = {
            budget_id: _NodeBalance(limit=limit) for budget_id, limit in limits.items()
        }
        self.res: dict[str, _ResState] = {}

    def _remaining(self, budget_id: str) -> int:
        node = self.nodes[budget_id]
        return node.limit - node.reserved - node.committed - node.overage

    def can_reserve(self, budget_id: str, estimate: int) -> bool:
        return estimate >= 0 and self._remaining(budget_id) >= estimate

    def apply_reserve(
        self, reservation_id: str, nodes: list[str], estimate: int, ttl: datetime, owner: str
    ) -> None:
        for budget_id in nodes:
            self.nodes[budget_id].reserved += estimate
        self.res[reservation_id] = _ResState(
            nodes=list(nodes), estimate=estimate, ttl=ttl, owner=owner
        )

    def apply_commit(self, reservation_id: str, actual: int) -> None:
        state = self.res[reservation_id]
        if state.status == "held":
            committed = min(actual, state.estimate)
            overage = max(actual - state.estimate, 0)
            for budget_id in state.nodes:
                node = self.nodes[budget_id]
                node.reserved -= state.estimate
                node.committed += committed
                node.overage += overage
        else:  # reaped -> self-healing late commit against each node's live remaining (§5.4)
            for budget_id in state.nodes:
                node = self.nodes[budget_id]
                remaining = max(node.limit - node.reserved - node.committed - node.overage, 0)
                committed = min(actual, remaining)
                node.committed += committed
                node.overage += actual - committed
        state.status = "committed"

    def apply_cancel(self, reservation_id: str) -> None:
        state = self.res[reservation_id]
        for budget_id in state.nodes:
            self.nodes[budget_id].reserved -= state.estimate
        state.status = "released"

    def apply_extend(self, reservation_id: str, ttl: datetime) -> None:
        state = self.res[reservation_id]
        state.ttl = max(state.ttl, ttl)

    def apply_reap(self, now: datetime) -> None:
        for state in self.res.values():
            if state.status == "held" and state.ttl <= now:
                for budget_id in state.nodes:
                    self.nodes[budget_id].reserved -= state.estimate
                state.status = "reaped"


async def _truncate_all(engine: AsyncEngine) -> None:
    """Truncate every data table this test owns (the ``committing_engine`` cleanup convention).

    ``_reset_and_seed`` truncates at the *start* of each Hypothesis example, but this module
    manages its own engine outside the ``db_conn``/``committing_engine`` fixtures, so nothing
    truncates after the *last* example. Left uncleaned, the final example's rows (e.g. the
    ``pb-1`` price book) leak into whatever integration test collects next in the same session.
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_DATA_TABLES} CASCADE"))


async def _reset_and_seed(engine: AsyncEngine) -> None:
    """Truncate every data table and seed the fixed price book + budget tree for one example."""
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_DATA_TABLES} CASCADE"))
        await conn.execute(price_book.insert().values(version="pb-1", published_at=_PERIOD))
        await conn.execute(
            price.insert().values(
                price_book_version="pb-1",
                provider="anthropic",
                model="claude",
                input_micro_per_token=Decimal("1"),
                output_micro_per_token=Decimal("2"),
                cached_input_micro_per_token=Decimal("0.5"),
                cache_creation_micro_per_token=Decimal("1.25"),
            )
        )
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(
            team.insert().values(
                [
                    {"team_id": "t1", "org_id": "o1", "name": "T1"},
                    {"team_id": "t2", "org_id": "o1", "name": "T2"},
                ]
            )
        )
        await conn.execute(
            user_principal.insert().values(
                [{"user_id": u, "team_id": t, "external_ref": None} for u, t in _USER_TEAM.items()]
            )
        )
        for budget_id, (scope_kind, scope_id, limit) in _BUDGETS.items():
            await conn.execute(
                budget.insert().values(
                    budget_id=budget_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    period_kind="calendar_month",
                    hard_limit_micro=limit,
                )
            )
            await conn.execute(
                budget_balance.insert().values(
                    budget_id=budget_id, period_start=_PERIOD, limit_micro=limit
                )
            )


def _auth_for(user: str) -> AuthContext:
    credential = Credential(
        credential_id=CredentialId(f"cred-{user}"),
        principal_id=PrincipalId(user),
        scope_kind=ScopeKind.USER,
        scope_id=user,
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(
        user_id=UserId(user), team_id=TeamId(_USER_TEAM[user]), org_id=OrgId("o1")
    )
    return AuthContext(credential=credential, principal=principal)


async def _db_balances(engine: AsyncEngine) -> dict[str, tuple[int, int, int, int]]:
    """Read every budget_balance row: budget_id -> (limit, reserved, committed, overage)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT budget_id, limit_micro, reserved_micro, committed_micro, overage_micro "
                "FROM budget_balance"
            )
        )
        return {
            str(row.budget_id): (
                int(row.limit_micro),
                int(row.reserved_micro),
                int(row.committed_micro),
                int(row.overage_micro),
            )
            for row in result
        }


class BudgetMachine(RuleBasedStateMachine):
    """Drives real reserve/commit/cancel/extend/reap over the fixed tree, mirroring a model."""

    reservations = Bundle("reservations")

    def __init__(self) -> None:
        super().__init__()
        assert _ENGINE is not None and _LOOP is not None
        self.engine = _ENGINE
        self.clock = _MutableClock(_START)
        self.model = _ReferenceModel({bid: limit for bid, (_, _, limit) in _BUDGETS.items()})
        self.counter = 0
        _run(_reset_and_seed(self.engine))
        uow = PostgresUnitOfWork(self.engine)
        ids = Uuid7IdGenerator()
        self.reserve_h = ReserveHandler(
            uow=uow, clock=self.clock, ids=ids, reservation_ttl_seconds=_TTL
        )
        self.commit_h = CommitHandler(uow=uow, ids=ids)
        self.cancel_h = CancelHandler(uow=uow, ids=ids)
        self.extend_h = ExtendHandler(uow=uow, clock=self.clock, reservation_ttl_seconds=_TTL)
        self.reaper = ReservationReaperHandler(uow=uow, clock=self.clock, ids=ids, batch_size=256)

    def _key(self) -> str:
        self.counter += 1
        return f"idem-{self.counter}"

    @rule(
        target=reservations,
        user=st.sampled_from(_USERS),
        inp=st.integers(min_value=0, max_value=500),
        out=st.integers(min_value=0, max_value=500),
    )
    def reserve(self, user: str, inp: int, out: int) -> object:
        estimate = estimate_micro(_PRICE, input_bound_tokens=inp, max_output_tokens=out)
        nodes = _APPLICABLE[user]
        will_succeed = all(self.model.can_reserve(b, estimate) for b in nodes)
        command = ReserveCommand(
            idempotency_key=self._key(),
            provider="anthropic",
            model="claude",
            input_bound_tokens=inp,
            max_output_tokens=out,
            labels={},
        )
        try:
            result = _run(self.reserve_h.reserve(_auth_for(user), command))
        except InsufficientBudget:
            assert not will_succeed, f"model expected {user} reserve of {estimate} to succeed"
            return multiple()
        assert will_succeed, f"model expected {user} reserve of {estimate} to be denied"
        self.model.apply_reserve(
            result.reservation_id, nodes, estimate, self.clock.now() + timedelta(seconds=_TTL), user
        )
        return result.reservation_id

    @rule(
        res=reservations,
        inp=st.integers(min_value=0, max_value=600),
        out=st.integers(min_value=0, max_value=600),
        cached=st.integers(min_value=0, max_value=600),
        cc=st.integers(min_value=0, max_value=200),
    )
    def commit(self, res: str, inp: int, out: int, cached: int, cc: int) -> None:
        cached = min(cached, inp)
        state = self.model.res[res]
        usage = ProviderUsage(
            input_tokens=inp,
            output_tokens=out,
            cached_input_tokens=cached,
            cache_creation_tokens=cc,
        )
        actual = actual_micro(
            _PRICE,
            input_tokens=inp,
            output_tokens=out,
            cached_input_tokens=cached,
            cache_creation_tokens=cc,
        )
        command = CommitCommand(
            idempotency_key=self._key(), reservation_id=ReservationId(res), usage=usage
        )
        auth = _auth_for(state.owner)
        if state.status in ("committed", "released"):
            with pytest.raises(ReservationNotHeld):
                _run(self.commit_h.commit(auth, command))
            return
        _run(self.commit_h.commit(auth, command))  # held -> reconcile; reaped -> self-heal
        self.model.apply_commit(res, actual)

    @rule(res=reservations)
    def cancel(self, res: str) -> None:
        state = self.model.res[res]
        command = CancelCommand(idempotency_key=self._key(), reservation_id=ReservationId(res))
        auth = _auth_for(state.owner)
        if state.status == "held":
            _run(self.cancel_h.cancel(auth, command))
            self.model.apply_cancel(res)
        else:
            with pytest.raises(ReservationNotHeld):
                _run(self.cancel_h.cancel(auth, command))

    @rule(res=reservations)
    def extend(self, res: str) -> None:
        state = self.model.res[res]
        command = ExtendCommand(reservation_id=ReservationId(res))
        auth = _auth_for(state.owner)
        if state.status == "held":
            result = _run(self.extend_h.extend(auth, command))
            self.model.apply_extend(res, result.ttl_deadline)
        else:
            with pytest.raises(ReservationNotHeld):
                _run(self.extend_h.extend(auth, command))

    @rule(advance=st.integers(min_value=0, max_value=_TTL * 2))
    def reap(self, advance: int) -> None:
        self.clock.advance(advance)
        _run(self.reaper.run_once())
        self.model.apply_reap(self.clock.now())

    @invariant()
    def db_matches_model(self) -> None:
        db = _run(_db_balances(self.engine))
        for budget_id, node in self.model.nodes.items():
            limit, reserved, committed, overage = db[budget_id]
            assert node_invariants_hold(
                Balance(
                    limit_micro=limit,
                    reserved_micro=reserved,
                    committed_micro=committed,
                    overage_micro=overage,
                )
            ), f"{budget_id} broke a per-node invariant: {db[budget_id]}"
            assert (reserved, committed, overage) == (
                node.reserved,
                node.committed,
                node.overage,
            ), f"{budget_id} DB {db[budget_id]} != model {node}"

    def teardown(self) -> None:
        report = _run(self._audit())
        assert report.ok, [(v.check, v.scope, v.detail) for v in report.violations]

    async def _audit(self) -> OracleReport:
        async with self.engine.connect() as conn:
            return await load_and_check(conn, checks=ALL_CHECKS - {Check.TREE_ROLLUP})


_SETTINGS = settings(
    max_examples=20,
    stateful_step_count=24,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    derandomize=True,
)


def test_budget_machine_upholds_invariants(postgres_url: str) -> None:
    """Run the model-based machine against the shared testcontainer through a private loop."""
    global _ENGINE, _LOOP
    loop = asyncio.new_event_loop()
    engine = create_async_engine(postgres_url, poolclass=pool.NullPool)
    _ENGINE, _LOOP = engine, loop
    try:
        from hypothesis.stateful import run_state_machine_as_test

        run_state_machine_as_test(  # type: ignore[no-untyped-call]  # hypothesis.stateful ships this entrypoint untyped
            BudgetMachine, settings=_SETTINGS
        )
    finally:
        loop.run_until_complete(_truncate_all(engine))
        loop.run_until_complete(engine.dispose())
        loop.close()
        _ENGINE = None
        _LOOP = None
