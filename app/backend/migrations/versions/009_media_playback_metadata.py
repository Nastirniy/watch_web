"""Codec and duration metadata for video and audio.

Adds media_files.duration_seconds, video_codec, audio_codec and playback_type
(the RFC 6381 string for HTMLMediaElement.canPlayType). Filled at upload by
the API, by the media worker while it extracts posters, and for existing rows
by `python -m app.backend.media_tools probe-missing`.

Guarded with inspector checks because 002_sync_schema adds the columns on
fresh installs.

Revision ID: 009_media_playback_metadata
Revises: 008_media_formats
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "009_media_playback_metadata"
down_revision = "008_media_formats"
branch_labels = None
depends_on = None

COLUMN_NAMES = ("duration_seconds", "video_codec", "audio_codec", "playback_type")


def _columns():
    return (
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("video_codec", sa.String(length=64), nullable=True),
        sa.Column("audio_codec", sa.String(length=64), nullable=True),
        sa.Column("playback_type", sa.String(length=255), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("media_files")}
    for column in _columns():
        if column.name not in existing:
            print(f"[009_media_playback_metadata] adding media_files.{column.name}")
            op.add_column("media_files", column)


def downgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("media_files")}
    with op.batch_alter_table("media_files") as batch:
        for name in COLUMN_NAMES:
            if name in existing:
                batch.drop_column(name)
