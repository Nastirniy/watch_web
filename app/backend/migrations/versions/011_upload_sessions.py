"""Resumable upload sessions.

upload_sessions tracks chunked uploads (POST /media/uploads): who uploads
what for which observation, how many bytes have arrived, and the outcome.
The part file lives under <upload_path>/_tmp/uploads until completion.

Guarded with an inspector check because 002_sync_schema creates the table
on fresh installs.

Revision ID: 011_upload_sessions
Revises: 010_archive_permissions
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "011_upload_sessions"
down_revision = "010_archive_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "upload_sessions" in sa.inspect(bind).get_table_names():
        return
    print("[011_upload_sessions] creating upload_sessions")
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("observations.id"), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=10), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("declared_content_type", sa.String(length=100), nullable=True),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("received_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("media_file_id", sa.Integer(), sa.ForeignKey("media_files.id"), nullable=True),
        sa.Column("part_path", sa.String(length=500), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("writer_token", sa.String(length=32), nullable=True),
        sa.Column("writer_since", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    for column in ("user_id", "observation_id", "post_id", "status", "expires_at"):
        op.create_index(f"ix_upload_sessions_{column}", "upload_sessions", [column])


def downgrade() -> None:
    bind = op.get_bind()
    if "upload_sessions" in sa.inspect(bind).get_table_names():
        op.drop_table("upload_sessions")
