"""Fence breaker dispatch and probe leases, including existing T-203 databases."""

import sqlalchemy as sa

from alembic import op

revision = "t203_fencing"
down_revision = "t203_circuit_breaker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("circuit_breakers")}
    if "epoch" not in columns:
        op.add_column(
            "circuit_breakers", sa.Column("epoch", sa.Integer(), nullable=False, server_default="0")
        )
    if "lease_until" not in columns:
        op.add_column("circuit_breakers", sa.Column("lease_until", sa.DateTime(), nullable=True))
    # Old half-open has no provable owner. Preserve it as quarantine, never dispatch.
    op.execute("UPDATE circuit_breakers SET state='open', open_until=NULL WHERE state='half_open'")


def downgrade() -> None:
    op.drop_column("circuit_breakers", "lease_until")
    op.drop_column("circuit_breakers", "epoch")
