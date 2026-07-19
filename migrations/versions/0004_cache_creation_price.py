"""cache creation price column

Adds the fourth cost-model token class (cache creation / cache-write) to the price
book. The rate may exceed the standard input rate — cache creation is billed at a
premium — so no CHECK ties it to the input rate; non-negativity is enforced in the
domain. Added NOT NULL via a transient server default so any pre-existing rows
backfill to 0, then the default is dropped so new inserts must state the rate
(matching the canonical model, which declares no default).

Revision ID: 0004_cache_creation_price
Revises: 0003_idempotency_created_at_ix
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_cache_creation_price"
down_revision = "0003_idempotency_created_at_ix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price",
        sa.Column(
            "cache_creation_micro_per_token",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("price", "cache_creation_micro_per_token", server_default=None)


def downgrade() -> None:
    op.drop_column("price", "cache_creation_micro_per_token")
