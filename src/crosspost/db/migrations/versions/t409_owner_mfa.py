"""Persist encrypted owner MFA state after authentication admission."""

import sqlalchemy as sa

from alembic import op

revision = "t409_owner_mfa"
down_revision = "t409_auth_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("users")}
    expected = {
        "mfa_secret": sa.Text,
        "mfa_last_step": sa.BigInteger,
        "mfa_backup_hashes": sa.Text,
        "mfa_changed_at": sa.BigInteger,
    }
    present = set(expected) & set(columns)
    if present and present != set(expected):
        raise RuntimeError("Partial owner MFA schema; migration refused")
    for name, kind in expected.items():
        if name in columns:
            if not isinstance(columns[name]["type"], kind) or not columns[name]["nullable"]:
                raise RuntimeError("Unrecognized owner MFA column; migration refused")
        else:
            op.add_column("users", sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a pre-MFA backup rather than discarding security state")
