"""Index bounded global error-spike telemetry without scanning historical log rows."""

from alembic import op

revision = "t304_log_health_index"
down_revision = "t203_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A legacy fixture (or manual schema maintenance) may already have this
    # index while Alembic is still behind. Upgrading must remain idempotent.
    op.execute("CREATE INDEX IF NOT EXISTS ix_logs_level_created_at ON logs (level, created_at)")


def downgrade() -> None:
    op.drop_index("ix_logs_level_created_at", table_name="logs")
