"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-22 00:00:00

Explicit DDL frozen from the canonical MetaData at this revision (#64). The baseline
must NOT run ``metadata.create_all``: that re-reads the live model on every fresh
install, so the moment a second migration exists a fresh install would build the
already-mutated model and then re-apply the delta (``DuplicateColumn``), and a model
edit made without a migration would silently diverge fresh installs from migrated
ones. Frozen DDL pins the baseline to exactly what the model was here;
``tests/integration/test_schema_migration.py`` reflects the migrated database and
asserts it still equals the model in full (columns, indexes, constraints), so any
future drift fails loudly instead of at a fresh install.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget",
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("period_kind", sa.Text(), nullable=False),
        sa.Column("period_len_days", sa.Integer(), nullable=True),
        sa.Column("hard_limit_micro", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), server_default=sa.text("'USD'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(period_kind = 'rolling_days' AND period_len_days IS NOT NULL AND "
            "period_len_days > 0) OR (period_kind = 'calendar_month' AND "
            "period_len_days IS NULL)",
            name=op.f("ck_budget_period_len_days_matches_kind"),
        ),
        sa.CheckConstraint(
            "period_kind IN ('calendar_month', 'rolling_days')",
            name=op.f("ck_budget_period_kind"),
        ),
        sa.CheckConstraint(
            "scope_kind IN ('org', 'team', 'user', 'project')",
            name=op.f("ck_budget_scope_kind"),
        ),
        sa.CheckConstraint("hard_limit_micro >= 0", name=op.f("ck_budget_hard_limit_non_negative")),
        sa.PrimaryKeyConstraint("budget_id", name=op.f("pk_budget")),
        sa.UniqueConstraint("scope_kind", "scope_id", name=op.f("uq_budget_scope_kind_scope_id")),
    )
    op.create_table(
        "idempotency_key",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("command_fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_idempotency_key")),
    )
    op.create_table(
        "org",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("org_id", name=op.f("pk_org")),
    )
    op.create_table(
        "price_book",
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_price_book")),
    )
    op.create_table(
        "budget_alert",
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("threshold_pct", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "threshold_pct > 0 AND threshold_pct <= 100",
            name=op.f("ck_budget_alert_threshold_pct_range"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_id"],
            ["budget.budget_id"],
            name=op.f("fk_budget_alert_budget_id_budget"),
        ),
        sa.PrimaryKeyConstraint("budget_id", "threshold_pct", name=op.f("pk_budget_alert")),
    )
    op.create_table(
        "budget_balance",
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limit_micro", sa.BigInteger(), nullable=False),
        sa.Column("reserved_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("committed_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("overage_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "committed_micro >= 0", name=op.f("ck_budget_balance_committed_non_negative")
        ),
        sa.CheckConstraint(
            "overage_micro >= 0", name=op.f("ck_budget_balance_overage_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_micro + committed_micro <= limit_micro",
            name=op.f("ck_budget_balance_reserved_committed_within_limit"),
        ),
        sa.CheckConstraint(
            "reserved_micro >= 0", name=op.f("ck_budget_balance_reserved_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["budget_id"],
            ["budget.budget_id"],
            name=op.f("fk_budget_balance_budget_id_budget"),
        ),
        sa.PrimaryKeyConstraint("budget_id", "period_start", name=op.f("pk_budget_balance")),
    )
    op.create_table(
        "price",
        sa.Column("price_book_version", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_micro_per_token", sa.Numeric(), nullable=False),
        sa.Column("output_micro_per_token", sa.Numeric(), nullable=False),
        sa.Column("cached_input_micro_per_token", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(
            ["price_book_version"],
            ["price_book.version"],
            name=op.f("fk_price_price_book_version_price_book"),
        ),
        sa.PrimaryKeyConstraint("price_book_version", "provider", "model", name=op.f("pk_price")),
    )
    op.create_table(
        "project",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.org_id"], name=op.f("fk_project_org_id_org")),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project")),
    )
    op.create_table(
        "team",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.org_id"], name=op.f("fk_team_org_id_org")),
        sa.PrimaryKeyConstraint("team_id", name=op.f("pk_team")),
    )
    op.create_table(
        "user_principal",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.team_id"], name=op.f("fk_user_principal_team_id_team")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_principal")),
    )
    op.create_table(
        "api_credential",
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_kind IN ('org', 'team', 'user', 'project')",
            name=op.f("ck_api_credential_scope_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name=op.f("ck_api_credential_status")
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["user_principal.user_id"],
            name=op.f("fk_api_credential_principal_id_user_principal"),
        ),
        sa.PrimaryKeyConstraint("credential_id", name=op.f("pk_api_credential")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_credential_token_hash")),
    )
    op.create_table(
        "reservation",
        sa.Column("reservation_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'held'"), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("price_book_version", sa.Text(), nullable=False),
        sa.Column("estimated_micro", sa.BigInteger(), nullable=False),
        sa.Column("input_bound_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ttl_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('held', 'committed', 'released', 'reaped')",
            name=op.f("ck_reservation_status"),
        ),
        sa.CheckConstraint(
            "estimated_micro >= 0", name=op.f("ck_reservation_estimated_non_negative")
        ),
        sa.CheckConstraint(
            "input_bound_tokens >= 0 AND max_output_tokens >= 0",
            name=op.f("ck_reservation_token_bounds_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["price_book_version"],
            ["price_book.version"],
            name=op.f("fk_reservation_price_book_version_price_book"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["user_principal.user_id"],
            name=op.f("fk_reservation_principal_id_user_principal"),
        ),
        sa.PrimaryKeyConstraint("reservation_id", name=op.f("pk_reservation")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_reservation_idempotency_key")),
    )
    op.create_index(
        "ix_reservation_status_ttl_deadline",
        "reservation",
        ["status", "ttl_deadline"],
        unique=False,
    )
    op.create_table(
        "ledger",
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reservation_id", sa.Text(), nullable=True),
        sa.Column(
            "delta_reserved_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "delta_committed_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "delta_overage_micro", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("price_book_version", sa.Text(), nullable=True),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('reserve', 'commit_adjust', 'release', 'reap', 'overage', 'grace_backfill')",
            name=op.f("ck_ledger_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_id"], ["budget.budget_id"], name=op.f("fk_ledger_budget_id_budget")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservation.reservation_id"],
            name=op.f("fk_ledger_reservation_id_reservation"),
        ),
        sa.PrimaryKeyConstraint("entry_id", name=op.f("pk_ledger")),
    )
    op.create_index("ix_ledger_budget_id_ts", "ledger", ["budget_id", "ts"], unique=False)
    op.create_table(
        "reservation_line",
        sa.Column("reservation_id", sa.Text(), nullable=False),
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["budget_id", "period_start"],
            ["budget_balance.budget_id", "budget_balance.period_start"],
            name=op.f("fk_reservation_line_budget_id_period_start_budget_balance"),
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservation.reservation_id"],
            name=op.f("fk_reservation_line_reservation_id_reservation"),
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id", "budget_id", "period_start", name=op.f("pk_reservation_line")
        ),
    )


def downgrade() -> None:
    op.drop_table("reservation_line")
    op.drop_index("ix_ledger_budget_id_ts", table_name="ledger")
    op.drop_table("ledger")
    op.drop_index("ix_reservation_status_ttl_deadline", table_name="reservation")
    op.drop_table("reservation")
    op.drop_table("api_credential")
    op.drop_table("user_principal")
    op.drop_table("team")
    op.drop_table("project")
    op.drop_table("price")
    op.drop_table("budget_balance")
    op.drop_table("budget_alert")
    op.drop_table("price_book")
    op.drop_table("org")
    op.drop_table("idempotency_key")
    op.drop_table("budget")
