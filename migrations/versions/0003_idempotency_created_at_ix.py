"""index idempotency_key.created_at for the reaper scan

Revision ID: 0003_idempotency_created_at_ix
Revises: 0002_idempotency_per_principal
Create Date: 2026-07-18 00:00:00

The idempotency-key reaper selects ``WHERE created_at < cutoff ORDER BY created_at``; without
an index that is a full table scan every tick, which outgrows the worker statement timeout once
a day's worth of keys accumulates and leaves the table growing without bound (#63). Add the
index so each reap batch is a bounded index range scan.
"""

from __future__ import annotations

from alembic import op

revision = "0003_idempotency_created_at_ix"
down_revision = "0002_idempotency_per_principal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_idempotency_key_created_at"),
        "idempotency_key",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_key_created_at"), table_name="idempotency_key")
