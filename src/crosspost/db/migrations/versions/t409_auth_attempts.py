"""Persist bounded authentication admission events after T-407."""

import sqlalchemy as sa

from alembic import op

revision = "t409_auth_attempts"
down_revision = "t407_client_invitations"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_auth_attempts_family_time": ["family", "occurred_at"],
    "ix_auth_attempts_ip_time": ["family", "ip_key", "occurred_at"],
    "ix_auth_attempts_subject_time": ["family", "subject_key", "occurred_at"],
    "ix_auth_attempts_time": ["occurred_at"],
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("auth_attempts"):
        columns = {c["name"]: c for c in inspector.get_columns("auth_attempts")}
        expected = {
            "id": sa.Integer,
            "family": sa.String,
            "ip_key": sa.String,
            "subject_key": sa.String,
            "occurred_at": sa.BigInteger,
        }
        existing_indexes = inspector.get_indexes("auth_attempts")
        indexes = {i["name"]: i["column_names"] for i in existing_indexes if not i["unique"]}
        if (
            set(columns) != set(expected)
            or any(
                not isinstance(columns[name]["type"], kind) or columns[name]["nullable"]
                for name, kind in expected.items()
            )
            or getattr(columns["family"]["type"], "length", None) != 32
            or getattr(columns["ip_key"]["type"], "length", None) != 64
            or getattr(columns["subject_key"]["type"], "length", None) != 64
            or inspector.get_pk_constraint("auth_attempts")["constrained_columns"] != ["id"]
            or indexes != _INDEXES
            or inspector.get_unique_constraints("auth_attempts")
            or inspector.get_foreign_keys("auth_attempts")
        ):
            raise RuntimeError("Unrecognized auth_attempts schema; migration refused")
        return
    op.create_table(
        "auth_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
    )
    for name, columns in _INDEXES.items():
        op.create_index(name, "auth_attempts", columns)


def downgrade() -> None:
    raise RuntimeError("Restore a pre-T409 backup instead of deleting security state")
