"""durable metered_receipt table

Adds ``metered_receipt``, a never-reaped idempotency record for the post-charge commands
(meter, grace backfill). Those commands apply spend with no reservation, so the reaped
``idempotency_key`` row was their only dedup and a retry after its TTL double-applied spend,
possibly in the wrong period (#92). This table shares ``idempotency_key``'s shape and
claim/replay semantics but is never reaped, so the exactly-once guarantee holds beyond any
retry window. Additive: no existing table or data is touched.

Revision ID: 0006_metered_receipt
Revises: 0005_meter_ledger
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_metered_receipt"
down_revision = "0005_meter_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metered_receipt",
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("command_fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("principal_id", "key", name=op.f("pk_metered_receipt")),
    )


def downgrade() -> None:
    op.drop_table("metered_receipt")
