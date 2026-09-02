"""
Standalone thumbnail generation worker.

Runs as a separate process (via systemd instantiated service) to generate
thumbnails without blocking the FastAPI server.

This worker is triggered by the media-watcher service when new media files
appear in the uploads directory. It:
  1. Looks up the MediaFile record by stored_filename (with retries)
  2. Generates a thumbnail using ThumbnailGenerator
  3. Updates the database with the thumbnail path

Usage:
    python -m app.backend.services.thumbnail_worker <stored_filename>

    stored_filename is the unique filename as stored in the DB, e.g.:
        42_abc123def456.mp4

    The full file path is resolved from the database record and
    settings.storage.upload_path. Set MYSETTINGS_STORAGE__UPLOAD_PATH to
    an absolute path in production to avoid CWD-dependent resolution.

Exit codes:
    0 - Thumbnail generated successfully
    1 - Error (file not found, DB error, generation failed)
    2 - Skipped (already has thumbnail, unsupported media type)
"""
import sys
import asyncio
import gc
from pathlib import Path

from sqlmodel import select

from app.backend.config import settings
from app.backend.database import get_session_maker, close_db
from app.backend.models.media import MediaFile
from app.backend.services.thumbnail_generator import ThumbnailGenerator
from app.backend.utils.logging_config import get_logger

_logger = get_logger("thumbnail_worker")

MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2


async def generate_thumbnail_for_file(stored_filename: str) -> int:
    """
    Look up the MediaFile record by stored_filename and generate a thumbnail.

    The file is identified by its stored_filename (unique in DB). The full
    path is resolved via media_file.resolve_path(storage_root) where
    storage_root comes from settings.storage.upload_path.

    Returns:
        0 on success, 1 on error, 2 on skip.
    """
    # Older watcher versions passed the full systemd-escaped path as the
    # instance name instead of just the stored filename. Normalize defensively.
    stored_filename = Path(stored_filename).name

    _logger.info(f"Processing: {stored_filename}")

    session_maker = await get_session_maker()

    # Retry DB lookup -- the record may not be committed yet when the
    # filesystem event fires (race condition with DB commit).
    media_file = None
    for attempt in range(1, MAX_RETRIES + 1):
        async with session_maker() as session:
            result = await session.exec(
                select(MediaFile).where(
                    MediaFile.stored_filename == stored_filename
                )
            )
            media_file = result.first()

        if media_file:
            break

        if attempt < MAX_RETRIES:
            _logger.info(
                f"MediaFile not found for {stored_filename}, "
                f"retrying ({attempt}/{MAX_RETRIES})..."
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    if not media_file:
        _logger.warning(f"No MediaFile record found for: {stored_filename}")
        return 1

    # Resolve storage root from config.
    # In production set MYSETTINGS_STORAGE__UPLOAD_PATH to an absolute path
    # so this is CWD-independent.
    storage_root = Path(settings.storage.upload_path)
    if not storage_root.is_absolute():
        storage_root = storage_root.resolve()

    # Verify source file exists before attempting generation
    source_path = media_file.resolve_path(storage_root)
    if not source_path.exists() or not source_path.is_file():
        _logger.error(f"Source file not found: {source_path}")
        return 1

    # Check if thumbnail already exists on disk
    if media_file.thumbnail_path:
        thumb = media_file.resolve_thumbnail_path(storage_root)
        if thumb and thumb.exists():
            _logger.info(f"Thumbnail already exists for media {media_file.id}")
            return 2

    thumbnail_path = None
    error_msg = None

    if media_file.media_type.lower() not in ("image", "video"):
        _logger.info(f"Skipping thumbnail for media type: {media_file.media_type}")
        error_msg = f"Unsupported media type: {media_file.media_type}"
    else:
        generator = ThumbnailGenerator(settings.storage.thumbnail, storage_root)
        _logger.info(
            f"Generating thumbnail for media {media_file.id} "
            f"({media_file.original_filename}, type={media_file.media_type})"
        )

        try:
            thumbnail_path = await generator.generate_thumbnail(
                media_file, size="medium"
            )
            if not thumbnail_path:
                error_msg = "External generation failed"
        except Exception as e:
            _logger.error(f"Thumbnail generation raised: {e}")
            error_msg = f"Generation error: {str(e)}"

    # Update DB
    async with session_maker() as session:
        db_file = await session.get(MediaFile, media_file.id)
        if not db_file:
            _logger.error(f"MediaFile {media_file.id} disappeared from DB")
            return 1

        if thumbnail_path:
            db_file.thumbnail_path = thumbnail_path
            db_file.thumbnail_error = None
            _logger.info(
                f"Thumbnail saved for media {media_file.id}: {thumbnail_path}"
            )
        else:
            db_file.thumbnail_error = error_msg
            _logger.error(
                f"Thumbnail generation failed/skipped for media {media_file.id}: {error_msg}"
            )

        await session.commit()

    return 0 if thumbnail_path else 2


async def main(stored_filename: str) -> int:
    try:
        return await generate_thumbnail_for_file(stored_filename)
    finally:
        await close_db()
        gc.collect()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage: python -m app.backend.services.thumbnail_worker <stored_filename>",
            file=sys.stderr,
        )
        sys.exit(1)

    exit_code = asyncio.run(main(sys.argv[1]))
    sys.exit(exit_code)