"""Drop posts.connection_status and posts.last_connection_at.

Nothing kept them up to date: last_connection_at was never written, and
connection_status was only a manual value set through PUT /posts/{id}
(offline, maintenance, error). Post presence comes from the home users'
sessions (Post.online_user_count, Post.last_user_activity_at).

Guarded with inspector checks: fresh installs built by 002_sync_schema from
the models never have these columns.

downgrade adds both columns back (connection_status = 'offline', last_connection_at
empty); the values dropped by upgrade are lost.

Revision ID: 014_drop_post_connection_columns
Revises: 013_observation_count_index
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "014_drop_post_connection_columns"
down_revision = "013_observation_count_index"
branch_labels = None
depends_on = None

TABLE = "posts"
COLUMNS = ("connection_status", "last_connection_at")


def _log(message: str) -> None:
    print(f"[014_drop_post_connection_columns] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = [c["name"] for c in inspector.get_columns(TABLE) if c["name"] in COLUMNS]
    if not present:
        return
    indexes = [
        idx["name"] for idx in inspector.get_indexes(TABLE)
        if idx.get("name") and set(idx.get("column_names") or []) & set(COLUMNS)
    ]
    with op.batch_alter_table(TABLE) as batch:
        for name in indexes:
            _log(f"dropping index {name}")
            batch.drop_index(name)
        for name in present:
            _log(f"dropping column {TABLE}.{name}")
            batch.drop_column(name)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        if "connection_status" not in present:
            batch.add_column(sa.Column("connection_status", sa.String(length=20), nullable=False, server_default="offline"))
            batch.create_index("ix_posts_connection_status", ["connection_status"])
        if "last_connection_at" not in present:
            batch.add_column(sa.Column("last_connection_at", sa.DateTime(), nullable=True))
            batch.create_index("ix_posts_last_connection_at", ["last_connection_at"])
