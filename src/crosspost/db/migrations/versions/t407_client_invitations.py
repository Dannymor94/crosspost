"""Persist one-use client invitation slots, after T-304."""

import sqlalchemy as sa

from alembic import op

revision = "t407_client_invitations"
down_revision = "t304_log_health_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("profile_name", sa.String(255), nullable=False),
        sa.Column("profile_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a pre-T407 backup rather than discarding invitation state")
