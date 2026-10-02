"""Relativize media file paths stored in the database.

Previously file_path and thumbnail_path were stored as absolute paths
(e.g. /old/server/uploads/2024/06/obs_001/42_abc.mp4).
After this migration they are stored relative to the storage root
(e.g. 2024/06/obs_001/42_abc.mp4), making the app portable across servers.

Revision ID: 001_relativize_media_paths
Revises: (none - first migration)
Create Date: 2026-06-08
"""
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "001_relativize_media_paths"
down_revision = "a2686fcb3afa"
branch_labels = None
depends_on = None

# Matches the date-structured portion of any absolute storage path.
# Pattern: YYYY/MM/DD/obs_NNN/<filename>
_DATE_PATTERN = re.compile(r"(\d{4}[/\\]\d{2}[/\\]\d{2}[/\\]obs_\d+[/\\].+)$")


def _to_relative(path_str: Optional[str], storage_root: Path) -> Optional[str]:
    """Convert an absolute stored path to relative, or leave relative paths unchanged."""
    if not path_str:
        return path_str

    p = Path(path_str)

    # Already relative - nothing to do.
    if not p.is_absolute():
        return path_str

    # Try to compute relative path against the current storage root.
    try:
        relative = p.relative_to(storage_root)
        return relative.as_posix()
    except ValueError:
        pass

    # Fallback: extract the date-structured portion using regex.
    # Handles the case where the old server had a different storage prefix.
    match = _DATE_PATTERN.search(path_str)
    if match:
        return PurePosixPath(match.group(1).replace("\\", "/")).as_posix()

    # Cannot determine relative path - log and keep as-is.
    print(f"WARNING: Could not relativize path: {path_str!r}")
    return path_str


def upgrade() -> None:
    bind = op.get_bind()

    # Fresh databases have no media_files table yet (002_sync_schema creates it).
    # Nothing to relativize in that case.
    if "media_files" not in sa.inspect(bind).get_table_names():
        print("INFO: media_files table does not exist yet, skipping 001_relativize_media_paths")
        return

    from app.backend.config import settings

    storage_root = Path(settings.storage.upload_path).resolve()

    # thumbnail_path is gone (006) in a table built from the current models (create_all, no alembic_version)
    columns = {c["name"] for c in sa.inspect(bind).get_columns("media_files")}
    if "thumbnail_path" not in columns:
        rows = bind.execute(text("SELECT id, file_path FROM media_files")).fetchall()
        for row in rows:
            new_file_path = _to_relative(row.file_path, storage_root)
            if new_file_path != row.file_path:
                bind.execute(text("UPDATE media_files SET file_path = :fp WHERE id = :id"), {"fp": new_file_path, "id": row.id})
        return

    rows = bind.execute(text("SELECT id, file_path, thumbnail_path FROM media_files")).fetchall()

    for row in rows:
        new_file_path = _to_relative(row.file_path, storage_root)
        new_thumb_path = _to_relative(row.thumbnail_path, storage_root)

        if new_file_path != row.file_path or new_thumb_path != row.thumbnail_path:
            bind.execute(
                text(
                    "UPDATE media_files "
                    "SET file_path = :fp, thumbnail_path = :tp "
                    "WHERE id = :id"
                ),
                {"fp": new_file_path, "tp": new_thumb_path, "id": row.id},
            )


def downgrade() -> None:
    # Downgrade is not implemented: converting relative paths back to absolute
    # would require knowing the original server's storage prefix, which is lost.
    raise NotImplementedError(
        "Downgrade of 001_relativize_media_paths is not supported. "
        "Restore from a database backup if you need to revert."
    )