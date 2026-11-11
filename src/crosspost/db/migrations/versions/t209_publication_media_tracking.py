"""publications: submitted_at + media_key (T-209)

Adds two nullable columns to track when a channel entered SUBMITTED state
and which media directory key is associated with the publication.

submitted_at — set explicitly in set_status when transitioning to SUBMITTED.
              used by TTL sweep to determine when to clean up submitted media.

media_key    — set via associate_media_key() after publish; survives
              scheduled_post hard-delete so TTL sweep can find orphaned media.
"""

import sqlalchemy as sa

from alembic import op

revision = "t209_publication_media_tracking"
down_revision = "t201_unify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("publications")}
        if "submitted_at" not in columns:
            batch_op.add_column(
                sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "media_key" not in columns:
            batch_op.add_column(sa.Column("media_key", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("media_key")
        batch_op.drop_column("submitted_at")
