"""User guides: the tables user_guides and user_guide_files (part 2, decision 7).

user_guides: title VARCHAR(200), description VARCHAR(2000) (plain text), sort_order INT default 0, audience_key
VARCHAR(24) (null = everyone, else a scope key g:<id> / p:<id>), is_deleted, created_at, updated_at, created_by,
updated_by. Index ix_user_guides_list (is_deleted, sort_order, title): the list every reader opens, active guides
in their order, filter and ORDER BY from one index.

user_guide_files: guide_id (foreign key to user_guides.id), file_name VARCHAR(255), stored_name VARCHAR(64),
file_path VARCHAR(500) (relative to the upload path), content_type VARCHAR(100), format_id INT (the media_formats
row the upload matched), size_bytes BIGINT, sha256 VARCHAR(64), language VARCHAR(8), is_deleted, created_at,
created_by. Index ix_user_guide_files_guide_id: the files of one guide; on MariaDB it is also the index the foreign
key needs (InnoDB drops the one it made for the key once this one exists).

No foreign key on the actor columns (created_by, updated_by: migration 028) nor on format_id (formats are archived,
never deleted, so the key would protect nothing and lock the format row on every insert). No CHECK constraints.

Both tables are listed in TABLES_OWNED_BY_LATER_REVISIONS of 002_sync_schema, and every create here is guarded with
an inspector check: where a table or index exists already (a database built with create_all, a second run) it is
left as it is.

Downgrade drops both tables (files first). The rows are lost; the files under <upload_path>/_guides/ stay on disk
(the output counts the rows).

Revision ID: 033_user_guides
Revises: 032_canonical_media_types
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa

revision = "033_user_guides"
down_revision = "032_canonical_media_types"
branch_labels = None
depends_on = None

GUIDES = "user_guides"
FILES = "user_guide_files"
INDEXES = {
    GUIDES: {"ix_user_guides_list": ["is_deleted", "sort_order", "title"]},
    FILES: {"ix_user_guide_files_guide_id": ["guide_id"]},
}


def _log(message: str) -> None:
    print(f"[033_user_guides] {message}")


def _inspector():
    return sa.inspect(op.get_bind())


def _create_guides() -> None:
    op.create_table(
        GUIDES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audience_key", sa.String(length=24), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
    )


def _create_files() -> None:
    op.create_table(
        FILES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guide_id", sa.Integer(), sa.ForeignKey(f"{GUIDES}.id"), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("format_id", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
    )


def upgrade() -> None:
    tables = set(_inspector().get_table_names())
    for table, create in ((GUIDES, _create_guides), (FILES, _create_files)):
        if table in tables:
            _log(f"{table} exists already, left as it is")
        else:
            _log(f"creating {table}")
            create()
    for table, indexes in INDEXES.items():
        existing = {index["name"] for index in _inspector().get_indexes(table)}
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table, columns)


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    bind = op.get_bind()
    for table in (FILES, GUIDES):
        if table in tables:
            count = bind.execute(sa.text(f"select count(*) from {table}")).scalar()
            _log(f"dropping {table} ({count} rows; files on disk stay)")
            op.drop_table(table)
