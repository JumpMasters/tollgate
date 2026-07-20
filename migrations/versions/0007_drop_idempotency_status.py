"""drop the vestigial idempotency status column

The idempotency ``status`` column was only ever written (``'succeeded'``) and never read: ``claim``
decides FRESH/REPLAY/MISMATCH from ``command_fingerprint`` and ``response`` alone. ADR 0007 had
reserved it for caching hard validation rejections, but the implementation only ever caches
successes — the safer policy, since a cached unknown-model rejection would pin a stale answer even
after a later price-book version adds the model. ADR 0038 supersedes ADR 0007 to "cache only
successes"; this drops the now-vestigial column from both idempotency tables (#96).

Revision ID: 0007_drop_idempotency_status
Revises: 0006_metered_receipt
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_drop_idempotency_status"
down_revision = "0006_metered_receipt"
branch_labels = None
depends_on = None

_TABLES = ("idempotency_key", "metered_receipt")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "status")


def downgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("status", sa.Text(), nullable=True))
