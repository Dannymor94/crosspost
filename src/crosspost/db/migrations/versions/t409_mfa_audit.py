"""Record owner MFA enrollment/replacement events atomically."""

import sqlalchemy as sa

from alembic import op

revision = "t409_mfa_audit"
down_revision = "t409_owner_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("owner_mfa_events"):
        columns = {c["name"]: c for c in inspector.get_columns("owner_mfa_events")}
        expected = {
            "id": sa.Integer,
            "user_id": sa.Integer,
            "action": sa.String,
            "generation": sa.Integer,
            "occurred_at": sa.BigInteger,
        }
        if (
            set(columns) != set(expected)
            or any(
                not isinstance(columns[name]["type"], kind) or columns[name]["nullable"]
                for name, kind in expected.items()
            )
            or getattr(columns["action"]["type"], "length", None) != 32
            or inspector.get_pk_constraint("owner_mfa_events")["constrained_columns"] != ["id"]
            or inspector.get_foreign_keys("owner_mfa_events")
            or inspector.get_unique_constraints("owner_mfa_events")
        ):
            raise RuntimeError("Unrecognized owner MFA audit schema; migration refused")
        return
    op.create_table(
        "owner_mfa_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a pre-MFA-audit backup; do not discard audit history")
