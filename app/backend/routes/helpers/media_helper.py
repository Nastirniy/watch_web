import asyncio
from pathlib import Path
from fastapi import HTTPException, status as fapi_status
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
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


def get_x_accel_redirect_path(media_file: MediaFile, media_manager) -> str:
    """Get the path for Nginx X-Accel-Redirect header"""
    symlink_path = media_manager.get_nginx_symlink_path(media_file)

    if not symlink_path:
        raise HTTPException(
            status_code=fapi_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Nginx streaming not available for this file type"
        )

    return f"{settings.storage.nginx_symlink_path}{symlink_path}"


def get_download_accel_redirect_path(media_file: MediaFile, media_manager) -> str:
    """
    Build the Nginx internal path for X-Accel-Redirect file downloads.
    Constructs: {nginx_internal_uploads_prefix}/{relative_path_from_uploads_dir}
    e.g. /protected_uploads/2025/06/15/obs_001/42_abc123.mp4
    """
    try:
        abs_path = media_file.resolve_path(Path(media_manager.storage_path))
        storage_root = Path(media_manager.storage_path).resolve()
        relative = abs_path.resolve().relative_to(storage_root)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cannot resolve file path relative to storage root"
        )

    prefix = settings.storage.nginx_internal_uploads_prefix.rstrip("/")
    return f"{prefix}/{relative.as_posix()}"


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
