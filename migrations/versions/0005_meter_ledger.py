"""meter ledger kind + self-describing columns

Admits the ``meter`` ledger kind (metering command) and adds ``model`` + ``labels`` so a
metering row — which has no reservation to join — is self-describing for chargeback rollups.
Both columns are nullable (no backfill); the CHECK is dropped and recreated because Postgres
cannot extend an IN-list constraint in place.

Revision ID: 0005_meter_ledger
Revises: 0004_cache_creation_price
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_meter_ledger"
down_revision = "0004_cache_creation_price"
branch_labels = None
depends_on = None

_KINDS_WITH_METER = (
    "kind IN ('reserve', 'commit_adjust', 'release', 'reap', 'overage', 'grace_backfill', 'meter')"
)
_KINDS_WITHOUT_METER = (
    "kind IN ('reserve', 'commit_adjust', 'release', 'reap', 'overage', 'grace_backfill')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ledger_kind"), "ledger", type_="check")
    op.create_check_constraint(op.f("ck_ledger_kind"), "ledger", _KINDS_WITH_METER)
    op.add_column("ledger", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("ledger", sa.Column("labels", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("ledger", "labels")
    op.drop_column("ledger", "model")
    op.drop_constraint(op.f("ck_ledger_kind"), "ledger", type_="check")
    op.create_check_constraint(op.f("ck_ledger_kind"), "ledger", _KINDS_WITHOUT_METER)
