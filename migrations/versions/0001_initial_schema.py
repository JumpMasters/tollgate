"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-22 00:00:00

The baseline builds the whole schema from the canonical MetaData, so the migration
and ``tollgate.adapters.postgres.schema`` are in sync by construction.
"""

from __future__ import annotations

from alembic import op

from tollgate.adapters.postgres.schema import metadata

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
