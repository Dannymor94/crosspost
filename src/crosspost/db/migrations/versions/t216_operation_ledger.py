"""Durable request admission ledger. Additive, not wired to HTTP yet."""

import sqlalchemy as sa

from alembic import op

revision = "t216_operation_ledger"
down_revision = "t203_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("publication_operations"):
        # A legacy Base.metadata.create_all database may already have this table.
        columns = {c["name"] for c in inspector.get_columns("publication_operations")}
        expected = {
            "operation_id",
            "profile_id",
            "actor_id",
            "kind",
            "fingerprint",
            "publication_id",
            "created_at",
        }
        pk = inspector.get_pk_constraint("publication_operations")["constrained_columns"]
        unique = inspector.get_unique_constraints("publication_operations")
        if (
            not expected <= columns
            or columns - expected - {"recovery_hash", "result_summary"}
            or pk != ["operation_id"]
            or not any(c["column_names"] == ["publication_id"] for c in unique)
        ):
            raise RuntimeError("Unrecognized publication operation ledger")
        return
    op.create_table(
        "publication_operations",
        sa.Column("operation_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("publication_id", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM publication_operations")).scalar()
    if count:
        raise RuntimeError("Refusing to discard publication operation tombstones")
    op.drop_table("publication_operations")
