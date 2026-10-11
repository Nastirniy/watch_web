"""posts.is_deleted: posts are archived like every other entity (POST /posts/{post_id}/archive and /restore).

An archived post leaves the data scope of its home users and of its user_post_access rows (only post.access_all
still reads its data) and its data is read-only; the rows that point at it are kept and apply again after a restore.
Existing posts are not archived (server default false).

Guarded with inspector checks: fresh installs built by 002_sync_schema from the models already have the column and
its index (the model declares the same type, NOT NULL and server default).

Revision ID: 020_post_archive
Revises: 019_certificate_cn_hash_scheme
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "020_post_archive"
down_revision = "019_certificate_cn_hash_scheme"
branch_labels = None
depends_on = None

TABLE = "posts"
COLUMN = "is_deleted"
INDEX = "ix_posts_is_deleted"


def _log(message: str) -> None:
    print(f"[020_post_archive] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE)}:
        _log(f"adding column {TABLE}.{COLUMN}")
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()))
    if INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}:
        _log(f"creating index {INDEX}")
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    """Drops the column: archived posts become live posts again (their users get their data back)."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if INDEX in {i["name"] for i in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
