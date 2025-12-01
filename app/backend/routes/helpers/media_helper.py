import asyncio
from pathlib import Path
from fastapi import HTTPException, status as fapi_status
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile, FileChunk
from app.backend.models.observation import Observation
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.utils.logging_config import get_api_logger
from app.backend.config import settings

_logger = get_api_logger()


async def get_observation_from_media(session: AsyncSession, media_file: MediaFile) -> Observation:
    """Helper to get observation and check if it exists"""
    obs_query = select(Observation).where(
        Observation.id == media_file.observation_id,
        Observation.is_deleted == False
    )
    result = await session.exec(obs_query)
    observation = result.first()

    if not observation:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Associated observation not found"
        )

    return observation


async def assemble_chunks_into_file(session: AsyncSession, media_file_id: int):
    """Assemble all chunks into a single complete file"""

    # Get media file info
    media_query = select(MediaFile).where(MediaFile.id == media_file_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        _logger.error(f"Media file {media_file_id} not found for assembly")
        return

    # Get all chunks ordered by chunk number
    chunks_query = select(FileChunk).where(
        FileChunk.media_file_id == media_file_id
    ).order_by(FileChunk.chunk_number)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()

    if not chunks:
        _logger.error(f"No chunks found for media file {media_file_id}")
        return

    try:
        # Create complete file path
        uploads_dir = Path(settings.storage.upload_path)
        observation_dir = uploads_dir / str(media_file.observation_id)
        complete_file_path = observation_dir / media_file.stored_filename

        # Ensure observation directory exists
        observation_dir.mkdir(parents=True, exist_ok=True)

        # Assemble chunks into complete file
        with open(complete_file_path, "wb") as complete_file:
            for chunk in chunks:
                try:
                    with open(chunk.chunk_file_path, "rb") as chunk_file:
                        chunk_data = chunk_file.read()
                        complete_file.write(chunk_data)
                except FileNotFoundError:
                    _logger.error(f"Chunk file not found: {chunk.chunk_file_path}")
                    return

        # Update media file with complete file path
        media_file.file_path = str(complete_file_path)
        media_file.upload_complete = True

        _logger.info(f"Successfully assembled {len(chunks)} chunks into {complete_file_path}")

    except Exception as e:
        _logger.error(f"Failed to assemble chunks for media file {media_file_id}: {e}")


async def cleanup_expired_upload_session(session: AsyncSession, media_file_id: int):
    """Clean up expired upload session - remove all chunks and reset media file"""

    # Get media file
    media_query = select(MediaFile).where(MediaFile.id == media_file_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        return

    # Get all chunks for this media file
    chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file_id)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()

    # Delete chunk files from filesystem
    for chunk in chunks:
        try:
            chunk_path = Path(chunk.chunk_file_path)
            if chunk_path.exists():
                chunk_path.unlink()
        except Exception as e:
            _logger.warning(f"Failed to delete expired chunk file {chunk.chunk_file_path}: {e}")

        # Delete chunk record from database
        await session.delete(chunk)

    # Clean up chunk directory
    try:
        uploads_dir = Path(settings.storage.upload_path)
        observation_dir = uploads_dir / str(media_file.observation_id)
        chunks_dir = observation_dir / f"media_{media_file_id}"

        if chunks_dir.exists():
            chunks_dir.rmdir()  # Only removes if empty
    except Exception as e:
        _logger.warning(f"Failed to cleanup expired chunk directory: {e}")

    # Reset media file session tracking
    media_file.upload_session_started = get_default_timezone_datetime()
    media_file.upload_last_activity = get_default_timezone_datetime()
    media_file.upload_complete = False

    _logger.info(f"Cleaned up expired upload session for media file {media_file_id}")


async def stream_media_file(session: AsyncSession, media_file: MediaFile, media_manager, validate_integrity: bool = True):
    """Stream large media files chunk by chunk"""

    async def generate_chunks():
        try:
            # Use the media manager for reliable retrieval
            file_data = await media_manager.retrieve_file(
                session=session,
                media_file_id=media_file.id,
                validate_integrity=validate_integrity
            )

            if not file_data:
                raise HTTPException(
                    status_code=fapi_status.HTTP_404_NOT_FOUND,
                    detail="File data not available"
                )

            # Stream in 64KB chunks for optimal performance
            chunk_size = 64 * 1024
            for i in range(0, len(file_data), chunk_size):
                chunk = file_data[i:i + chunk_size]
                yield chunk

                # Small delay to prevent overwhelming the client
                await asyncio.sleep(0.001)

        except Exception as e:
            _logger.error(f"Error streaming media file {media_file.id}: {e}")
            raise HTTPException(
                status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to stream file"
            )

    return StreamingResponse(
        generate_chunks(),
        media_type=media_file.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename={media_file.original_filename}",
            "Content-Length": str(media_file.file_size),
            "X-File-Hash": media_file.file_hash,
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
            "ETag": f'"{media_file.file_hash}"'
        }
    )