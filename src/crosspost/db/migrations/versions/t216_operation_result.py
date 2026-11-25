"""Add optional recovery proof and immutable result summary to T-216 ledger."""

import sqlalchemy as sa

from alembic import op

revision = "t216_operation_result"
down_revision = "t216_operation_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("publication_operations")}
    if "recovery_hash" not in columns:
        op.add_column(
            "publication_operations", sa.Column("recovery_hash", sa.String(64), nullable=True)
        )
    if "result_summary" not in columns:
        op.add_column(
            "publication_operations", sa.Column("result_summary", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM publication_operations WHERE recovery_hash IS NOT NULL")
    ).scalar()
    if count:
        raise RuntimeError("Refusing to discard operation recovery evidence")
    op.drop_column("publication_operations", "result_summary")
    op.drop_column("publication_operations", "recovery_hash")
