"""scope idempotency keys per principal

Revision ID: 0002_idempotency_per_principal
Revises: 0001_initial_schema
Create Date: 2026-07-18 00:00:00

Scope idempotency to the acting principal (#71) and align the reservation-side
idempotency guard with it (#61):

- ``idempotency_key`` gains a ``principal_id`` column and its primary key becomes
  ``(principal_id, key)``, so two tenants that choose the same key string no longer
  collide (a global key namespace is a cross-tenant denial-of-service surface). The
  column is added with a temporary ``''`` default so any in-flight rows satisfy NOT
  NULL across the repivot; the default is then dropped, so new claims must supply the
  principal explicitly (the model carries no default).
- ``reservation``'s unique idempotency guard becomes ``(principal_id, idempotency_key)``
  to match, so the reserve path can map a reaped-key reuse to a 409 instead of a 500.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_idempotency_per_principal"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "idempotency_key",
        sa.Column("principal_id", sa.Text(), nullable=False, server_default=""),
    )
    op.drop_constraint(op.f("pk_idempotency_key"), "idempotency_key", type_="primary")
    op.create_primary_key(op.f("pk_idempotency_key"), "idempotency_key", ["principal_id", "key"])
    op.alter_column("idempotency_key", "principal_id", server_default=None)

    op.drop_constraint(op.f("uq_reservation_idempotency_key"), "reservation", type_="unique")
    op.create_unique_constraint(
        op.f("uq_reservation_principal_id_idempotency_key"),
        "reservation",
        ["principal_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_reservation_principal_id_idempotency_key"), "reservation", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_reservation_idempotency_key"), "reservation", ["idempotency_key"]
    )
    op.drop_constraint(op.f("pk_idempotency_key"), "idempotency_key", type_="primary")
    op.create_primary_key(op.f("pk_idempotency_key"), "idempotency_key", ["key"])
    op.drop_column("idempotency_key", "principal_id")
