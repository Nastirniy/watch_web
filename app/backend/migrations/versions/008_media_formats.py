"""Accepted media formats become a managed table.

media_formats holds the upload allowlist (Content-Type, extensions, aliases,
per-format size limit) that used to be a constant in the code. Rows are
seeded from DEFAULT_FORMATS at application start (seed_database), so this
revision only creates the table. Guarded with an inspector check because
002_sync_schema creates it on fresh installs.

Revision ID: 008_media_formats
Revises: 007_unescape_html_entities
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "008_media_formats"
down_revision = "007_unescape_html_entities"
branch_labels = None
depends_on = None


def _log(message: str) -> None:
    print(f"[008_media_formats] {message}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "media_formats" in inspector.get_table_names():
        return
    _log("creating media_formats")
    op.create_table(
        "media_formats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_type", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False, unique=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("extensions", sa.JSON(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("max_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_media_formats_media_type", "media_formats", ["media_type"])
    op.create_index("ix_media_formats_is_active", "media_formats", ["is_active"])
    op.create_index("ix_media_formats_is_deleted", "media_formats", ["is_deleted"])


def downgrade() -> None:
    bind = op.get_bind()
    if "media_formats" in sa.inspect(bind).get_table_names():
        op.drop_table("media_formats")
