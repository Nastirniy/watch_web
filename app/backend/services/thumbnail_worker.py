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
    python -m app.backend.services.thumbnail_worker /path/to/media/file.mp4

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


async def generate_thumbnail_for_file(file_path: str) -> int:
    """
    Look up the MediaFile record by stored_filename and generate a thumbnail.

    The file is identified by its stored_filename (unique in DB), not the full
    path. This avoids issues with relative vs absolute path mismatches.

    Returns:
        0 on success, 1 on error, 2 on skip.
    """
    source = Path(file_path)
    if not source.exists() or not source.is_file():
        _logger.error(f"File not found: {file_path}")
        return 1

    stored_filename = source.name
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

    # Override the relative DB path with the verified absolute path so that
    # ThumbnailGenerator can find the source file regardless of CWD.
    media_file.file_path = str(source.resolve())

    # Check if thumbnail already exists on disk
    if media_file.thumbnail_path:
        thumb = Path(media_file.thumbnail_path)
        if thumb.exists():
            _logger.info(f"Thumbnail already exists for media {media_file.id}")
            return 2

    # Preparation for status update
    thumbnail_path = None
    error_msg = None

    # Check if media type supports thumbnails
    if media_file.media_type.lower() not in ("image", "video"):
        _logger.info(f"Skipping thumbnail for media type: {media_file.media_type}")
        error_msg = f"Unsupported media type: {media_file.media_type}"
    else:
        # Generate thumbnail
        generator = ThumbnailGenerator(settings.storage.thumbnail)
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
        # Re-fetch to avoid stale data
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


async def main(file_path: str) -> int:
    try:
        return await generate_thumbnail_for_file(file_path)
    finally:
        await close_db()
        gc.collect()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            f"Usage: python -m app.backend.services.thumbnail_worker <file_path>",
            file=sys.stderr,
        )
        sys.exit(1)

    exit_code = asyncio.run(main(sys.argv[1]))
    sys.exit(exit_code)
