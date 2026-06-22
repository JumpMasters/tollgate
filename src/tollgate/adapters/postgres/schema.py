"""SQLAlchemy Core schema: the budget ledger of record (spec §3).

This module is the single source of truth for the relational schema. The Alembic
baseline migration builds it with ``metadata.create_all`` and ``env.py`` points
Alembic's ``target_metadata`` here, so the migration and the models cannot drift.
All money is integer micro-USD (``BigInteger``); per-token price rates are
``Numeric`` (Decimal); timestamps are timezone-aware. Enum-like columns
(``scope_kind``, ``status``, ``kind``, ``period_kind``) are ``Text`` with named
CHECK constraints whose allowed values come from the tuples below; a unit test
asserts those tuples match the domain enums where one exists.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

#: Deterministic constraint names (stable for Alembic and assertable in tests).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

#: Allowed values for the enum-like text columns. Single source for the CHECK SQL;
#: SCOPE_KINDS and RESERVATION_STATUSES are asserted equal to the domain enums by a
#: unit test (PERIOD_KINDS / CREDENTIAL_STATUSES / LEDGER_KINDS have no domain enum yet).
SCOPE_KINDS = ("org", "team", "user", "project")
PERIOD_KINDS = ("calendar_month", "rolling_days")
CREDENTIAL_STATUSES = ("active", "revoked")
RESERVATION_STATUSES = ("held", "committed", "released", "reaped")
LEDGER_KINDS = ("reserve", "commit_adjust", "release", "reap", "overage", "grace_backfill")


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """A named ``CHECK (column IN (...))`` built from an allowed-value tuple."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


org = Table(
    "org",
    metadata,
    Column("org_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
)

team = Table(
    "team",
    metadata,
    Column("team_id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("org.org_id"), nullable=False),
    Column("name", Text, nullable=False),
)

user_principal = Table(
    "user_principal",
    metadata,
    Column("user_id", Text, primary_key=True),
    Column("team_id", Text, ForeignKey("team.team_id"), nullable=False),
    Column("external_ref", Text, nullable=True),
)

project = Table(
    "project",
    metadata,
    Column("project_id", Text, primary_key=True),
    Column("org_id", Text, ForeignKey("org.org_id"), nullable=False),
    Column("key", Text, nullable=False),
)

api_credential = Table(
    "api_credential",
    metadata,
    Column("credential_id", Text, primary_key=True),
    Column("principal_id", Text, ForeignKey("user_principal.user_id"), nullable=False),
    Column("scope_kind", Text, nullable=False),
    Column("scope_id", Text, nullable=False),
    Column("token_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _enum_check("scope_kind", SCOPE_KINDS, "scope_kind"),
    _enum_check("status", CREDENTIAL_STATUSES, "status"),
    UniqueConstraint("token_hash"),
)

price_book = Table(
    "price_book",
    metadata,
    Column("version", Text, primary_key=True),
    Column("published_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

price = Table(
    "price",
    metadata,
    Column("price_book_version", Text, ForeignKey("price_book.version"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_micro_per_token", Numeric, nullable=False),
    Column("output_micro_per_token", Numeric, nullable=False),
    Column("cached_input_micro_per_token", Numeric, nullable=False),
    PrimaryKeyConstraint("price_book_version", "provider", "model"),
)

budget = Table(
    "budget",
    metadata,
    Column("budget_id", Text, primary_key=True),
    Column("scope_kind", Text, nullable=False),
    Column("scope_id", Text, nullable=False),
    Column("period_kind", Text, nullable=False),
    Column("period_len_days", Integer, nullable=True),
    Column("hard_limit_micro", BigInteger, nullable=False),
    Column("currency", Text, nullable=False, server_default=text("'USD'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _enum_check("scope_kind", SCOPE_KINDS, "scope_kind"),
    _enum_check("period_kind", PERIOD_KINDS, "period_kind"),
    CheckConstraint("hard_limit_micro >= 0", name="hard_limit_non_negative"),
    CheckConstraint(
        "(period_kind = 'rolling_days' AND period_len_days IS NOT NULL AND period_len_days > 0)"
        " OR (period_kind = 'calendar_month' AND period_len_days IS NULL)",
        name="period_len_days_matches_kind",
    ),
    # ADR 0025: at most one budget per scope node in V1.
    UniqueConstraint("scope_kind", "scope_id"),
)

budget_alert = Table(
    "budget_alert",
    metadata,
    Column("budget_id", Text, ForeignKey("budget.budget_id"), nullable=False),
    Column("threshold_pct", Integer, nullable=False),
    CheckConstraint("threshold_pct > 0 AND threshold_pct <= 100", name="threshold_pct_range"),
    PrimaryKeyConstraint("budget_id", "threshold_pct"),
)

budget_balance = Table(
    "budget_balance",
    metadata,
    Column("budget_id", Text, ForeignKey("budget.budget_id"), nullable=False),
    Column("period_start", DateTime(timezone=True), nullable=False),
    Column("limit_micro", BigInteger, nullable=False),
    Column("reserved_micro", BigInteger, nullable=False, server_default=text("0")),
    Column("committed_micro", BigInteger, nullable=False, server_default=text("0")),
    Column("overage_micro", BigInteger, nullable=False, server_default=text("0")),
    CheckConstraint("reserved_micro >= 0", name="reserved_non_negative"),
    CheckConstraint("committed_micro >= 0", name="committed_non_negative"),
    CheckConstraint("overage_micro >= 0", name="overage_non_negative"),
    CheckConstraint(
        "reserved_micro + committed_micro <= limit_micro",
        name="reserved_committed_within_limit",
    ),
    PrimaryKeyConstraint("budget_id", "period_start"),
)

reservation = Table(
    "reservation",
    metadata,
    Column("reservation_id", Text, primary_key=True),
    Column("idempotency_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'held'")),
    Column("principal_id", Text, ForeignKey("user_principal.user_id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("price_book_version", Text, ForeignKey("price_book.version"), nullable=False),
    Column("estimated_micro", BigInteger, nullable=False),
    Column("input_bound_tokens", BigInteger, nullable=False),
    Column("max_output_tokens", BigInteger, nullable=False),
    Column("labels", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ttl_deadline", DateTime(timezone=True), nullable=False),
    _enum_check("status", RESERVATION_STATUSES, "status"),
    CheckConstraint("estimated_micro >= 0", name="estimated_non_negative"),
    CheckConstraint(
        "input_bound_tokens >= 0 AND max_output_tokens >= 0",
        name="token_bounds_non_negative",
    ),
    UniqueConstraint("idempotency_key"),
    Index("ix_reservation_status_ttl_deadline", "status", "ttl_deadline"),
)

reservation_line = Table(
    "reservation_line",
    metadata,
    Column("reservation_id", Text, ForeignKey("reservation.reservation_id"), nullable=False),
    Column("budget_id", Text, nullable=False),
    Column("period_start", DateTime(timezone=True), nullable=False),
    Column("amount_micro", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["budget_id", "period_start"],
        ["budget_balance.budget_id", "budget_balance.period_start"],
    ),
    PrimaryKeyConstraint("reservation_id", "budget_id", "period_start"),
)

ledger = Table(
    "ledger",
    metadata,
    Column("entry_id", Text, primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("kind", Text, nullable=False),
    Column("budget_id", Text, ForeignKey("budget.budget_id"), nullable=False),
    Column("period_start", DateTime(timezone=True), nullable=False),
    Column("reservation_id", Text, ForeignKey("reservation.reservation_id"), nullable=True),
    Column("delta_reserved_micro", BigInteger, nullable=False, server_default=text("0")),
    Column("delta_committed_micro", BigInteger, nullable=False, server_default=text("0")),
    Column("delta_overage_micro", BigInteger, nullable=False, server_default=text("0")),
    Column("actual_input_tokens", BigInteger, nullable=True),
    Column("actual_output_tokens", BigInteger, nullable=True),
    Column("provider", Text, nullable=True),
    Column("price_book_version", Text, nullable=True),
    Column("ref", Text, nullable=True),
    _enum_check("kind", LEDGER_KINDS, "kind"),
    Index("ix_ledger_budget_id_ts", "budget_id", "ts"),
)

idempotency_key = Table(
    "idempotency_key",
    metadata,
    Column("key", Text, primary_key=True),
    Column("command_fingerprint", Text, nullable=False),
    Column("status", Text, nullable=True),
    Column("response", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
