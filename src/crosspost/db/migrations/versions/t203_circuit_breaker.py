"""Add circuit_breakers table. T-203."""

import sqlalchemy as sa

from alembic import op

revision = "t203_circuit_breaker"
down_revision = "t201_unify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "circuit_breakers" not in tables:
        op.create_table(
            "circuit_breakers",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("failure_count", sa.Integer(), nullable=False),
            sa.Column("open_until", sa.DateTime(timezone=False), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
            sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("profile_id", "channel", name="uq_circuit_breaker"),
        )
        op.create_index("ix_circuit_breakers_profile_id", "circuit_breakers", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_circuit_breakers_profile_id", table_name="circuit_breakers")
    op.drop_table("circuit_breakers")
