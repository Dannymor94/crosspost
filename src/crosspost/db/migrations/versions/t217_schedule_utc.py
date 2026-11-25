"""Add nullable canonical UTC instant without rewriting any legacy time."""

import sqlalchemy as sa

from alembic import op

revision = "t217_schedule_utc"
down_revision = "t203_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_posts")}
    if "scheduled_at_utc" not in columns:
        op.add_column("scheduled_posts", sa.Column("scheduled_at_utc", sa.DateTime()))


def downgrade() -> None:
    stmt = sa.text("SELECT COUNT(*) FROM scheduled_posts WHERE scheduled_at_utc IS NOT NULL")
    if op.get_bind().execute(stmt).scalar():
        raise RuntimeError("Refusing to discard authoritative UTC schedule instants")
    op.drop_column("scheduled_posts", "scheduled_at_utc")
