"""Video poster job state and worker heartbeats.

Thumbnails are no longer precomputed: imgproxy renders them from the original
on demand. The only derived file left is one poster frame per video, extracted
by the media worker, and this revision adds its job state to media_files:

- poster_status, poster_attempts, poster_locked_until, poster_worker,
  poster_generated_at, poster_error
- index ix_media_files_poster_status
- table worker_heartbeats

Existing videos are queued (poster_status = pending) so the worker regenerates
every poster on its first run; the old *_thumb_medium.jpg files are not reused.

Every step is guarded with an inspector check because 002_sync_schema already
creates the columns and the table on fresh installs.

Revision ID: 005_video_posters
Revises: 004_role_soft_delete
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "005_video_posters"
down_revision = "004_role_soft_delete"
branch_labels = None
depends_on = None

POSTER_COLUMN_NAMES = (
    "poster_status", "poster_attempts", "poster_locked_until",
    "poster_worker", "poster_generated_at", "poster_error",
)


def _poster_columns():
    """Fresh Column objects on every call (a Column can only be attached to one table)"""
    return (
        sa.Column("poster_status", sa.String(length=12), nullable=True),
        sa.Column("poster_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poster_locked_until", sa.DateTime(), nullable=True),
        sa.Column("poster_worker", sa.String(length=64), nullable=True),
        sa.Column("poster_generated_at", sa.DateTime(), nullable=True),
        sa.Column("poster_error", sa.String(length=500), nullable=True),
    )
POSTER_STATUS_INDEX = "ix_media_files_poster_status"


def _log(message: str) -> None:
    print(f"[005_video_posters] {message}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = {c["name"] for c in inspector.get_columns("media_files")}
    for column in _poster_columns():
        if column.name in existing:
            continue
        _log(f"adding media_files.{column.name}")
        op.add_column("media_files", column)

    indexes = {i["name"] for i in inspector.get_indexes("media_files")}
    if POSTER_STATUS_INDEX not in indexes:
        op.create_index(POSTER_STATUS_INDEX, "media_files", ["poster_status"])

    if "worker_heartbeats" not in inspector.get_table_names():
        _log("creating worker_heartbeats")
        op.create_table(
            "worker_heartbeats",
            sa.Column("name", sa.String(length=64), primary_key=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("hostname", sa.String(length=128), nullable=False),
            sa.Column("pid", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("jobs_done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("jobs_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("details", sa.String(length=500), nullable=True),
        )
        op.create_index("ix_worker_heartbeats_kind", "worker_heartbeats", ["kind"])

    queued = bind.execute(
        sa.text("UPDATE media_files SET poster_status = 'pending' WHERE media_type = 'video' AND poster_status IS NULL")
    ).rowcount
    if queued:
        _log(f"queued {queued} existing video(s) for poster extraction")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "worker_heartbeats" in inspector.get_table_names():
        op.drop_table("worker_heartbeats")
    if POSTER_STATUS_INDEX in {i["name"] for i in inspector.get_indexes("media_files")}:
        op.drop_index(POSTER_STATUS_INDEX, table_name="media_files")
    existing = {c["name"] for c in inspector.get_columns("media_files")}
    with op.batch_alter_table("media_files") as batch:
        for name in POSTER_COLUMN_NAMES:
            if name in existing:
                batch.drop_column(name)
