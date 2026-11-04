"""Схема после ручных миграций, T-201. Определения заморожены."""

import sqlalchemy as sa

from alembic import op

revision = "t201_unify"
down_revision = "85b6d4dfff8f"
branch_labels = None
depends_on = None


def _create_missing_tables():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "checkpoints" not in tables:
        op.create_table(
            "checkpoints",
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("publication_id", sa.String(length=64), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("target", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint("profile_id", "publication_id", "channel", "target"),
        )
    if "channel_targets" not in tables:
        op.create_table(
            "channel_targets",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("value_blob", sa.Text(), nullable=False),
            sa.Column("topic_id", sa.Integer(), nullable=True),
            sa.Column("topic_title", sa.String(length=255), nullable=True),
            sa.Column("peer_access_hash", sa.BigInteger(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if "channel_targets" not in tables:
        op.create_index(
            "ix_channel_target", "channel_targets", ["profile_id", "channel"], unique=False
        )
    if "channel_targets" not in tables:
        op.create_index(
            op.f("ix_channel_targets_profile_id"), "channel_targets", ["profile_id"], unique=False
        )
    if "profile_settings" not in tables:
        op.create_table(
            "profile_settings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("value", sa.JSON(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("profile_id", "key", name="uq_profile_setting"),
        )
    if "profile_settings" not in tables:
        op.create_index(
            op.f("ix_profile_settings_profile_id"), "profile_settings", ["profile_id"], unique=False
        )
    if "scheduled_posts" not in tables:
        op.create_table(
            "scheduled_posts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("content_type", sa.String(length=32), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("article_text", sa.Text(), nullable=True),
            sa.Column("link_phrase", sa.String(length=128), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("media_paths", sa.JSON(), nullable=False),
            sa.Column("channels", sa.JSON(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if "scheduled_posts" not in tables:
        op.create_index(
            op.f("ix_scheduled_posts_profile_id"), "scheduled_posts", ["profile_id"], unique=False
        )
    if "scheduled_posts" not in tables:
        op.create_index(
            "ix_scheduled_report",
            "scheduled_posts",
            ["profile_id", "status", "published_at"],
            unique=False,
        )
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("login", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    if "users" not in tables:
        op.create_index(op.f("ix_users_login"), "users", ["login"], unique=True)
    if "users" not in tables:
        op.create_index(op.f("ix_users_profile_id"), "users", ["profile_id"], unique=False)


def upgrade():
    _create_missing_tables()
    additions: dict[str, list[sa.Column]] = {
        "connections": [sa.Column("expires_at", sa.DateTime(timezone=True))],
        "scheduled_posts": [
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("article_text", sa.Text()),
            sa.Column("link_phrase", sa.String(128)),
            sa.Column("reject_reason", sa.Text()),
        ],
        "channel_targets": [
            sa.Column("topic_id", sa.Integer()),
            sa.Column("topic_title", sa.String(255)),
            sa.Column("peer_access_hash", sa.BigInteger()),
        ],
    }
    for table, new_columns in additions.items():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for column in new_columns:
            if column.name not in existing:
                op.add_column(table, column)
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("scheduled_posts")}
    if "ix_scheduled_report" not in indexes:
        op.create_index(
            "ix_scheduled_report", "scheduled_posts", ["profile_id", "status", "published_at"]
        )

    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("publications")}
    if "target" not in columns:
        with op.batch_alter_table("publications") as batch:
            batch.add_column(sa.Column("target", sa.String(64), nullable=False, server_default=""))
            batch.drop_constraint("uq_publication", type_="unique")
            batch.create_unique_constraint(
                "uq_publication", ["profile_id", "publication_id", "channel", "target"]
            )
    # Старый ручной rebuild оставлял nullable timestamps и SQL defaults.
    op.execute("UPDATE publications SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    op.execute(
        "UPDATE publications SET updated_at=COALESCE(created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL"
    )
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("publications")}
    # Старый raw SQL объявлял REFERENCES inline. SQLite reflection может потерять
    # ON DELETE, поэтому одного recreate="always" недостаточно: восстанавливаем
    # действие явно в копируемой таблице, сохраняя остальные constraints/индексы.
    publications = sa.Table("publications", sa.MetaData(), autoload_with=op.get_bind())
    for constraint in publications.foreign_key_constraints:
        if constraint.column_keys == ["profile_id"] and [
            fk.target_fullname for fk in constraint.elements
        ] == ["profiles.id"]:
            constraint.ondelete = "CASCADE"
            for fk in constraint.elements:
                fk.ondelete = "CASCADE"
    with op.batch_alter_table("publications", recreate="always", copy_from=publications) as batch:
        for name in ("target", "attempt_count"):
            if columns[name]["default"] is not None:
                batch.alter_column(name, server_default=None)
        if columns["created_at"]["nullable"] or columns["created_at"]["default"] is None:
            batch.alter_column(
                "created_at", nullable=False, server_default=sa.func.current_timestamp()
            )
        if columns["updated_at"]["nullable"]:
            batch.alter_column("updated_at", nullable=False)

    op.execute(
        "UPDATE scheduled_posts SET status='failed' "
        "WHERE status='published' AND EXISTS ("
        "SELECT 1 FROM publications p "
        "WHERE p.publication_id = 'schedpost-' || scheduled_posts.id "
        "AND p.profile_id = scheduled_posts.profile_id "
        "AND p.status NOT IN ('done','submitted'))"
    )
    op.execute("UPDATE scheduled_posts SET status='done' WHERE status='published'")


def downgrade():
    raise RuntimeError("T-201 is irreversible: restore the database backup to roll back")
