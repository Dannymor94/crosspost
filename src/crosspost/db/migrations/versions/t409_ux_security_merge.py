"""Validation-only merge of UX migration branch and owner security chain."""

revision = "t409_ux_security_merge"
down_revision = ("t304_ux_auth_merge", "t409_mfa_audit")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("Restore a compatible pre-integration backup")
