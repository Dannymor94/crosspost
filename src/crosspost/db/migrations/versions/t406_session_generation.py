"""Persist per-user session generation for owner-controlled recovery (T-406)."""

import sqlalchemy as sa

from alembic import op

revision = "t406_session_generation"
down_revision = "t203_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "session_generation" not in columns:
        op.add_column(
            "users",
            sa.Column("session_generation", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("users", "session_generation")
