"""Video storyboards: table video_storyboards (job state and layout, one row per video).

Guarded with inspector checks: fresh installs built by 002_sync_schema never create it (it is listed in
TABLES_OWNED_BY_LATER_REVISIONS), a rerun finds it. Downgrade drops the table and leaves the files on disk.

Revision ID: 017_video_storyboards
Revises: 016_session_certificate_binding
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa

revision = "017_video_storyboards"
down_revision = "016_session_certificate_binding"
branch_labels = None
depends_on = None

TABLE = "video_storyboards"
INDEX = "ix_video_storyboards_status_locked"


def _log(message: str) -> None:
    print(f"[017_video_storyboards] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        _log(f"creating {TABLE}")
        op.create_table(
            TABLE,
            sa.Column("media_id", sa.Integer(), sa.ForeignKey("media_files.id"), primary_key=True),
            sa.Column("status", sa.String(length=12), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("locked_until", sa.DateTime(), nullable=True),
            sa.Column("worker", sa.String(length=64), nullable=True),
            sa.Column("error", sa.String(length=500), nullable=True),
            sa.Column("generated_at", sa.DateTime(), nullable=True),
            sa.Column("step_seconds", sa.Integer(), nullable=True),
            sa.Column("frame_count", sa.Integer(), nullable=True),
            sa.Column("columns", sa.Integer(), nullable=True),
            sa.Column("rows", sa.Integer(), nullable=True),
            sa.Column("tile_width", sa.Integer(), nullable=True),
            sa.Column("tile_height", sa.Integer(), nullable=True),
            sa.Column("sheet_count", sa.Integer(), nullable=True),
        )
        inspector = sa.inspect(op.get_bind())
    if INDEX not in {i["name"] for i in inspector.get_indexes(TABLE)}:
        _log(f"creating index {INDEX}")
        op.create_index(INDEX, TABLE, ["status", "locked_until"])


def downgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
