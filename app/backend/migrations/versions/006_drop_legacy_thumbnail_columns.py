"""Drop the legacy thumbnail columns.

thumbnail_path and thumbnail_error described precomputed *_thumb_medium.jpg
files. Thumbnails are now rendered on demand (images) or derived from the
poster tracked by the poster_* columns (videos), so nothing reads these
columns any more. The files themselves are removed with:

    python -m app.backend.media_tools cleanup-legacy-thumbnails

Revision ID: 006_drop_legacy_thumbnails
Revises: 005_video_posters
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "006_drop_legacy_thumbnails"
down_revision = "005_video_posters"
branch_labels = None
depends_on = None

LEGACY_COLUMNS = ("thumbnail_path", "thumbnail_error")


def _log(message: str) -> None:
    print(f"[006_drop_legacy_thumbnails] {message}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("media_files")}
    to_drop = [name for name in LEGACY_COLUMNS if name in existing]
    if not to_drop:
        return
    with op.batch_alter_table("media_files") as batch:
        for name in to_drop:
            _log(f"dropping media_files.{name}")
            batch.drop_column(name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("media_files")}
    for name in LEGACY_COLUMNS:
        if name not in existing:
            op.add_column("media_files", sa.Column(name, sa.String(length=500), nullable=True))
