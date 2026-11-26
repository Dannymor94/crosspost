"""Private revisioned drafts; no backfill or publication side effects."""

import sqlalchemy as sa

from alembic import op

revision = "t218_editor_draft"
down_revision = "t203_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "editor_drafts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "editor_drafts",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "profile_id", sa.Integer(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("revision", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_editor_drafts_user_id", "editor_drafts", ["user_id"])
    op.create_index("ix_editor_drafts_profile_id", "editor_drafts", ["profile_id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM editor_drafts")).scalar():
        raise RuntimeError("Refusing to discard editor drafts")
    op.drop_table("editor_drafts")
