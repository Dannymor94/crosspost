"""Persist one-use client invitation slots, after T-304."""

import sqlalchemy as sa

from alembic import op

revision = "t407_client_invitations"
down_revision = "t304_log_health_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("client_invitations"):
        # Legacy adoption may replay revisions on an already upgraded schema.
        # Accept only the expected table contract; never silently stamp a
        # partial or incompatible invitation table.
        columns = {c["name"]: c for c in inspector.get_columns("client_invitations")}
        expected = {
            "id": (sa.Integer, False),
            "profile_id": (sa.Integer, False),
            "profile_name": (sa.String, False),
            "profile_created_at": (sa.DateTime, False),
            "token_hash": (sa.String, False),
            "expires_at": (sa.BigInteger, False),
            "consumed_at": (sa.BigInteger, True),
        }
        constraints = inspector.get_unique_constraints("client_invitations")
        unique = {tuple(c["column_names"] or ()) for c in constraints}
        foreign = inspector.get_foreign_keys("client_invitations")
        if (
            set(columns) != set(expected)
            or any(
                not isinstance(columns[name]["type"], kind)
                or columns[name]["nullable"] != nullable
                for name, (kind, nullable) in expected.items()
            )
            or getattr(columns["profile_name"]["type"], "length", None) != 255
            or getattr(columns["token_hash"]["type"], "length", None) != 64
            or inspector.get_pk_constraint("client_invitations")["constrained_columns"] != ["id"]
            or unique != {("profile_id",), ("token_hash",)}
            or len(foreign) != 1
            or foreign[0]["constrained_columns"] != ["profile_id"]
            or foreign[0]["referred_table"] != "profiles"
            or foreign[0]["referred_columns"] != ["id"]
            or foreign[0]["options"].get("ondelete") != "CASCADE"
        ):
            raise RuntimeError("Unrecognized client_invitations schema; migration refused")
        return
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
