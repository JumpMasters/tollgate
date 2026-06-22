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
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

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
