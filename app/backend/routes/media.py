from typing import List, Optional
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import asyncio
import gc
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse, Response
from sqlmodel import select, desc
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
import io

from app.backend.database import get_session, get_session_maker
from app.backend.models.media import MediaFile, FileChunk
from app.backend.models.observation import Observation
from app.backend.models.user import User
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.services.access_control import AccessControlService
from app.backend.services.media_manager import MediaManager, FileIntegrityCheck, FileValidationConfig, UploadSession
from app.backend.enums.general import Permissions, MediaType
from app.backend.enums.routes import MediaRoutes, SystemRoutes
from app.backend.utils.mime_detector import mime_detector
from app.backend.utils.logging_config import get_logger, get_api_logger
from app.backend.config import settings
from app.backend.routes.models.request.media_request import ChunkedUploadStart, ResumeUploadRequest, ListAttachedMediaRequest
from app.backend.routes.models.response.media_response import (
    ChunkedUploadResponse,
    ChunkUploadResponse,
    MediaFileInfo,
    UploadSessionInfo,
    ThumbnailInfo
)
from app.backend.routes.helpers.media_helper import (
    get_observation_from_media,
    assemble_chunks_into_file,
    cleanup_expired_upload_session,
    stream_media_file
)
from app.backend.services.thumbnail_generator import ThumbnailGenerator

router = APIRouter()
_logger = get_api_logger()


# @router.post(MediaRoutes.UPLOAD_CHUNKED_START.value, response_model=ChunkedUploadResponse)
# @PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
# async def start_chunked_upload(
#     upload_data: ChunkedUploadStart,
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """
#     Start a chunked file upload session.
#     Users can only upload media for observations from their assigned post.
#     """
#
#     # Verify observation exists and user has access
#     observation_query = select(Observation).where(
#         Observation.id == upload_data.observation_id,
#         Observation.is_deleted == False
#     )
#     observation = await session.exec(observation_query)
#     observation_result = observation.first()
#
#     if not observation_result:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="Observation not found"
#         )
#
#     # Check if user has access to this observation's post
#     await AccessControlService.check_post_membership(
#         current_user,
#         observation_result.post_id,
#         session,
#         f"{Permissions.OBSERVATION_CREATE.name} for {MediaRoutes.UPLOAD_CHUNKED_START.value}"
#     )
#
#     # Validate media type
#     try:
#         media_type_enum = MediaType(upload_data.media_type.lower())
#     except ValueError:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_400_BAD_REQUEST,
#             detail=f"Invalid media type: {upload_data.media_type}"
#         )
#
#     # Generate unique file hash for deduplication and resume
#     timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
#     file_hash_base = f"{upload_data.original_filename}_{upload_data.file_size}_{timestamp}"
#     file_hash = hashlib.sha256(file_hash_base.encode()).hexdigest()[:16]
#
#     # Generate MD5 hash of original filename
#     md5_hash = hashlib.md5(upload_data.original_filename.encode()).hexdigest()[:16]
#
#     # Generate unique part using timestamp and file_hash
#     unique_part = f"{timestamp}_{file_hash[:8]}"
#
#     # Create temporary stored filename (will be updated after getting media_file.id)
#     temp_stored_filename = f"{unique_part}_{md5_hash}{upload_data.file_extension}"
#
#     # Create media file record
#     media_file = MediaFile(
#         observation_id=upload_data.observation_id,
#         original_filename=upload_data.original_filename,
#         stored_filename=temp_stored_filename,
#         file_path=f"{settings.storage.upload_path}/{observation_result.id}/{temp_stored_filename}",
#         file_size=upload_data.file_size,
#         mime_type="application/octet-stream",  # Will be updated when first chunk arrives
#         file_extension=upload_data.file_extension,
#         media_type=media_type_enum.value,
#         file_hash=file_hash
#     )
#
#     session.add(media_file)
#     await session.commit()
#     await session.refresh(media_file)
#
#     # Update stored filename with media_file.id
#     stored_filename = f"{media_file.id}_{md5_hash}{upload_data.file_extension}"
#     media_file.stored_filename = stored_filename
#     media_file.file_path = f"{settings.storage.upload_path}/{observation_result.id}/{stored_filename}"
#     await session.commit()
#     await session.refresh(media_file)
#
#     upload_id = f"{media_file.id}_{file_hash}"
#
#     return ChunkedUploadResponse(
#         upload_id=upload_id,
#         media_file_id=media_file.id,
#         chunk_size=upload_data.chunk_size,
#         total_chunks=upload_data.total_chunks,
#         uploaded_chunks=[]
#     )


# @router.post(MediaRoutes.UPLOAD_CHUNKED_CHUNK.value, response_model=ChunkUploadResponse)
# @PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
# async def upload_chunk(
#     upload_id: str = Form(...),
#     chunk_number: int = Form(...),
#     chunk_file: UploadFile = File(...),
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user),
#     background_tasks: BackgroundTasks = BackgroundTasks()
# ):
#     """
#     Upload a single chunk of a file.
#     Users can only upload chunks for media from their assigned post.
#     """
#
#     # Parse upload_id to get media_file_id
#     try:
#         media_file_id = int(upload_id.split('_')[0])
#     except (ValueError, IndexError):
#         raise HTTPException(
#             status_code=fapi_status.HTTP_400_BAD_REQUEST,
#             detail="Invalid upload_id format"
#         )
#
#     # Get media file
#     media_query = select(MediaFile).where(MediaFile.id == media_file_id)
#     media_result = await session.exec(media_query)
#     media_file = media_result.first()
#
#     if not media_file:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="Media file not found"
#         )
#
#     # Check if user has access to this media's observation post
#     observation = await get_observation_from_media(session, media_file)
#     await AccessControlService.check_post_membership(
#         current_user,
#         observation.post_id,
#         session,
#         f"{Permissions.OBSERVATION_CREATE.name} for {MediaRoutes.UPLOAD_CHUNKED_CHUNK.value}"
#     )
#
#     # Check upload session timeout (30 minutes default)
#     current_time = get_default_timezone_datetime()
#     timeout_duration = timedelta(minutes=settings.storage.upload_timeout_minutes)
#
#     # Ensure upload_last_activity has timezone info before comparison
#     last_activity = media_file.upload_last_activity
#     if last_activity.tzinfo is None:
#         last_activity = last_activity.replace(tzinfo=timezone.utc)
#
#     if current_time - last_activity > timeout_duration:
#         # Session expired, clean up chunks and restart
#         await cleanup_expired_upload_session(session, media_file_id)
#         raise HTTPException(
#             status_code=fapi_status.HTTP_408_REQUEST_TIMEOUT,
#             detail=f"Upload session expired after {settings.storage.upload_timeout_minutes} minutes. Please restart upload."
#         )
#
#     # Update last activity time
#     media_file.upload_last_activity = current_time
#
#     # Check if chunk already exists
#     chunk_query = select(FileChunk).where(
#         FileChunk.media_file_id == media_file_id,
#         FileChunk.chunk_number == chunk_number
#     )
#     existing_chunk = await session.exec(chunk_query)
#     if existing_chunk.first():
#         # Chunk already uploaded, skip
#         pass
#     else:
#         # Read and validate chunk data
#         chunk_data = await chunk_file.read()
#         chunk_size = len(chunk_data)
#         chunk_hash = hashlib.sha256(chunk_data).hexdigest()
#
#         # Create uploads directory structure
#         uploads_dir = Path(settings.storage.upload_path)
#         observation_dir = uploads_dir / str(media_file.observation_id)
#         chunks_dir = observation_dir / f"media_{media_file_id}"
#
#         chunks_dir.mkdir(parents=True, exist_ok=True)
#
#         # Save chunk to filesystem
#         chunk_filename = f"chunk_{chunk_number:04d}.bin"
#         chunk_file_path = chunks_dir / chunk_filename
#
#         with open(chunk_file_path, "wb") as f:
#             f.write(chunk_data)
#
#         # Detect mime type from first chunk using advanced detection
#         if chunk_number == 0:
#             detected_mime, detection_method = mime_detector.detect_mime_type(
#                 chunk_data,
#                 media_file.original_filename
#             )
#
#             media_file.mime_type = detected_mime
#
#             # Log detection method for debugging
#             _logger.info(f"MIME type detected for {media_file.original_filename}: {detected_mime} (method: {detection_method})")
#
#         # Create chunk record with file path
#         file_chunk = FileChunk(
#             media_file_id=media_file_id,
#             chunk_number=chunk_number,
#             chunk_size=chunk_size,
#             chunk_hash=chunk_hash,
#             chunk_file_path=str(chunk_file_path)
#         )
#
#         session.add(file_chunk)
#
#     # Get all uploaded chunks
#     all_chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file_id)
#     all_chunks = await session.exec(all_chunks_query)
#     uploaded_chunks = [chunk.chunk_number for chunk in all_chunks.all()]
#
#     # Calculate expected total chunks based on file size
#     expected_chunks = (media_file.file_size + 1048576 - 1) // 1048576  # 1MB chunks
#     remaining_chunks = expected_chunks - len(uploaded_chunks)
#     upload_complete = remaining_chunks == 0
#
#     # If upload is complete, assemble the chunks into a single file
#     if upload_complete:
#         await assemble_chunks_into_file(session, media_file_id)
#
#         # Trigger background thumbnail generation for images and videos
#         if media_file.media_type.lower() in ['image', 'video']:
#             background_tasks.add_task(generate_thumbnail_background, media_file_id, session)
#
#     await session.commit()
#
#     return ChunkUploadResponse(
#         chunk_number=chunk_number,
#         uploaded=True,
#         remaining_chunks=remaining_chunks,
#         upload_complete=upload_complete
#     )


@router.get(MediaRoutes.MEDIA_INFO.value, response_model=MediaFileInfo)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def get_media_info(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Get information about a media file and its upload status.
    Users can only access media from their assigned post.
    """

    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Get observation and check post access
    observation = await get_observation_from_media(session, media_file)
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.MEDIA_INFO.value}"
    )
    
    # Get chunk information
    chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_id)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()
    
    expected_chunks = (media_file.file_size + 1048576 - 1) // 1048576  # 1MB chunks
    upload_complete = len(chunks) == expected_chunks
    
    return MediaFileInfo(
        id=media_file.id,
        original_filename=media_file.original_filename,
        stored_filename=media_file.stored_filename,
        file_size=media_file.file_size,
        mime_type=media_file.mime_type,
        media_type=media_file.media_type,
        width=media_file.width,
        height=media_file.height,
        uploaded_at=media_file.uploaded_at,
        upload_complete=upload_complete,
        total_chunks=expected_chunks,
        uploaded_chunks=len(chunks)
    )


@router.get(MediaRoutes.MEDIA_DOWNLOAD.value)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def download_media_file(
    media_id: int,
    validate_integrity: bool = True,
    stream: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Download a complete media file with integrity validation and optional streaming.
    Users can only download media from their assigned post.
    """

    # Get media file info first
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post
    observation = await get_observation_from_media(session, media_file)
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.MEDIA_DOWNLOAD.value}"
    )

    # Handle streaming download for large files
    if stream and media_file.file_size > 10 * 1024 * 1024:  # 10MB threshold
        return await stream_media_file(session, media_file, media_manager, validate_integrity)
    
    # Use the enhanced media manager for retrieval
    try:
        file_data = await media_manager.retrieve_file(
            session=session,
            media_file_id=media_id,
            validate_integrity=validate_integrity
        )

        if not file_data:
            raise HTTPException(
                status_code=fapi_status.HTTP_404_NOT_FOUND,
                detail="Media file not found or could not be retrieved"
            )

        return Response(
            content=file_data,
            media_type=media_file.mime_type,
            headers={
                "Content-Disposition": f"attachment; filename={media_file.original_filename}",
                "Content-Length": str(len(file_data)),
                "X-File-Hash": media_file.file_hash
            }
        )

    except ValueError as e:
        _logger.exception(f"File integrity validation failed: {str(e)}")

        # Handle integrity validation failures
        if "integrity" in str(e).lower():
            raise HTTPException(
                status_code=fapi_status.HTTP_409_CONFLICT,
                detail=f"File integrity validation failed"
            )
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Server side error"  # TODO swap to real error message
        )
    except Exception as e:
        _logger.error(f"Failed to download media file {media_id}: {e}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve file"
        )


@router.get(SystemRoutes.ROOT.value, response_model=List[MediaFileInfo])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_media_files(
    observation_id: Optional[int] = None,
    media_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """List media files with optional filtering"""
    
    query = select(MediaFile)
    
    if observation_id:
        query = query.where(MediaFile.observation_id == observation_id)
    
    if media_type:
        query = query.where(MediaFile.media_type == media_type)
    
    query = query.offset(skip).limit(limit).order_by(desc(MediaFile.uploaded_at))
    
    result = await session.exec(query)
    media_files = result.all()
    
    # Get chunk information for each file
    media_info_list = []
    for media_file in media_files:
        chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file.id)
        chunks_result = await session.exec(chunks_query)
        chunks = chunks_result.all()
        
        expected_chunks = (media_file.file_size + 1048576 - 1) // 1048576
        upload_complete = len(chunks) == expected_chunks
        
        media_info_list.append(MediaFileInfo(
            id=media_file.id,
            original_filename=media_file.original_filename,
            stored_filename=media_file.stored_filename,
            file_size=media_file.file_size,
            mime_type=media_file.mime_type,
            media_type=media_file.media_type,
            width=media_file.width,
            height=media_file.height,
            uploaded_at=media_file.uploaded_at,
            upload_complete=upload_complete,
            total_chunks=expected_chunks,
            uploaded_chunks=len(chunks)
        ))
    
    return media_info_list


@router.delete(MediaRoutes.MEDIA_BY_ID.value)
@PermissionService.require_permissions({
    "all": [
        {"any": [Permissions.OBSERVATION_DELETE.value, Permissions.IS_ADMIN.value]},
        Permissions.OBSERVATION_VIEW.value
    ]
})
async def delete_media_file(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a media file and all its chunks.
    Users can only delete media from their assigned post.
    """

    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post
    observation = await get_observation_from_media(session, media_file)
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_DELETE.name} for {MediaRoutes.MEDIA_BY_ID.value}"
    )

    # Delete all chunks first (due to foreign key constraint)
    chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_id)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()

    for chunk in chunks:
        # Delete chunk file from filesystem
        try:
            chunk_path = Path(chunk.chunk_file_path)
            if chunk_path.exists():
                chunk_path.unlink()
        except Exception as e:
            _logger.warning(f"Failed to delete chunk file {chunk.chunk_file_path}: {e}")
        
        # Delete chunk record from database
        await session.delete(chunk)
    
    # Try to remove empty directories
    try:
        uploads_dir = Path(settings.storage.upload_path)
        observation_dir = uploads_dir / str(media_file.observation_id)
        chunks_dir = observation_dir / f"media_{media_id}"
        
        if chunks_dir.exists():
            chunks_dir.rmdir()  # Only removes if empty
            
        # Try to remove observation directory if empty
        if observation_dir.exists() and not any(observation_dir.iterdir()):
            observation_dir.rmdir()
    except Exception as e:
        _logger.warning(f"Failed to cleanup directories: {e}")
    
    # Delete media file record
    await session.delete(media_file)
    await session.commit()
    
    return {"message": "Media file deleted successfully"}


# Initialize media manager instance with enterprise features
media_manager = MediaManager(
    use_filesystem_cache=True,
    auto_cleanup=True,
    enable_deduplication=True,
    storage_path=getattr(settings.storage, 'upload_path', 'uploads'),
    temp_path=getattr(settings.storage, 'temp_path', 'temp')
)


# @router.get(MediaRoutes.FILE_INTEGRITY.value, response_model=FileIntegrityCheck)
# @PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
# async def check_file_integrity(
#     media_id: int,
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """
#     Check file integrity and detect any issues.
#     Users can only check integrity for media from their assigned post.
#     """
#
#     # Get media file first
#     media_query = select(MediaFile).where(MediaFile.id == media_id)
#     media_result = await session.exec(media_query)
#     media_file = media_result.first()
#
#     if not media_file:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="Media file not found"
#         )
#
#     # Check if user has access to this media's observation post
#     observation = await get_observation_from_media(session, media_file)
#     await AccessControlService.check_post_membership(
#         current_user,
#         observation.post_id,
#         session,
#         f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.FILE_INTEGRITY.value}"
#     )
#
#     integrity = await media_manager.check_file_integrity(session, media_id)
#     return integrity


# @router.post(MediaRoutes.FILE_REPAIR.value)
# @PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
# async def repair_file(
#     media_id: int,
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """Attempt to repair a corrupted or incomplete file"""
#
#     success = await media_manager.repair_file(session, media_id)
#
#     if success:
#         return {"message": f"File {media_id} repaired successfully"}
#     else:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
#             detail="File could not be repaired"
#         )


# @router.post(MediaRoutes.MAINTENANCE_CLEANUP.value)
# @PermissionService.require_permission(Permissions.IS_ADMIN.value)
# async def cleanup_orphaned_files(
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """Clean up orphaned files and database records"""
#
#     db_cleaned, fs_cleaned = await media_manager.cleanup_orphaned_files(session)
#
#     return {
#         "message": "Cleanup completed",
#         "database_records_cleaned": db_cleaned,
#         "filesystem_files_cleaned": fs_cleaned
#     }


@router.get(MediaRoutes.STORAGE_STATISTICS.value)
@PermissionService.require_permission(Permissions.IS_ADMIN.value)
async def get_storage_statistics(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Get storage statistics and health information"""
    
    stats = await media_manager.get_storage_statistics(session)
    return stats


# @router.post(MediaRoutes.CHECK_UPLOAD_SESSION.value, response_model=Optional[UploadSessionInfo])
# @PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
# async def check_upload_session(
#     request: ResumeUploadRequest,
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """
#     Check if an upload session exists and can be resumed.
#     Users can only check sessions for their assigned post.
#     """
#
#     # Verify observation exists and user has access
#     observation_query = select(Observation).where(
#         Observation.id == request.observation_id,
#         Observation.is_deleted == False
#     )
#     observation = await session.exec(observation_query)
#     observation_result = observation.first()
#
#     if not observation_result:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="Observation not found"
#         )
#
#     # Check if user has access to this observation's post
#     await AccessControlService.check_post_membership(
#         current_user,
#         observation_result.post_id,
#         session,
#         f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.CHECK_UPLOAD_SESSION.value}"
#     )
#
#     # Check for existing upload session
#     upload_session = await media_manager.check_existing_upload(
#         session, request.file_hash, request.observation_id
#     )
#
#     if upload_session:
#         return UploadSessionInfo(
#             media_file_id=upload_session.media_file_id,
#             uploaded_chunks=upload_session.uploaded_chunks,
#             total_chunks=upload_session.total_chunks,
#             file_hash=upload_session.file_hash,
#             last_activity=upload_session.last_activity,
#             is_expired=upload_session.is_expired,
#             can_resume=upload_session.can_resume
#         )
#
#     return None


# @router.post(MediaRoutes.RESUME_UPLOAD.value)
# @PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
# async def resume_upload_session(
#     request: ResumeUploadRequest,
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """Resume an existing upload session"""
#
#     # Check for existing upload session
#     upload_session = await media_manager.check_existing_upload(
#         session, request.file_hash, request.observation_id
#     )
#
#     if not upload_session:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="No upload session found for this file"
#         )
#
#     if not upload_session.can_resume:
#         if upload_session.is_expired:
#             raise HTTPException(
#                 status_code=fapi_status.HTTP_408_REQUEST_TIMEOUT,
#                 detail="Upload session has expired"
#             )
#         else:
#             raise HTTPException(
#                 status_code=fapi_status.HTTP_409_CONFLICT,
#                 detail="Upload session is complete or cannot be resumed"
#             )
#
#     try:
#         media_file_id = await media_manager.resume_upload(session, upload_session)
#         return {
#             "message": "Upload session resumed successfully",
#             "media_file_id": media_file_id,
#             "uploaded_chunks": upload_session.uploaded_chunks,
#             "total_chunks": upload_session.total_chunks
#         }
#     except Exception as e:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_400_BAD_REQUEST,
#             detail=f"Failed to resume upload: {str(e)}"
#         )


# @router.post(MediaRoutes.CLEANUP_OLD_CHUNKS.value)
# @PermissionService.require_permission(Permissions.IS_ADMIN.value)
# async def cleanup_old_chunks(
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """Clean up old chunks based on retention policy"""
#
#     chunks_cleaned = await media_manager.cleanup_old_chunks(session)
#
#     return {
#         "message": "Old chunk cleanup completed",
#         "chunks_cleaned": chunks_cleaned
#     }


@router.post(MediaRoutes.UPLOAD.value)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def upload_complete_file(
    observation_id: int = Form(...),
    media_type: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Upload a complete file with comprehensive validation and security checks.
    Users can only upload files for observations from their assigned post.
    """

    # Verify observation exists
    observation_query = select(Observation).where(
        Observation.id == observation_id,
        Observation.is_deleted == False
    )
    observation = await session.exec(observation_query)
    observation_result = observation.first()

    if not observation_result:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    # Check if user has access to this observation's post
    await AccessControlService.check_post_membership(
        current_user,
        observation_result.post_id,
        session,
        f"{Permissions.OBSERVATION_CREATE.name} for {MediaRoutes.UPLOAD.value}"
    )

    # Validate file size before reading (basic check)
    if file.size and file.size > 500 * 1024 * 1024:  # 500MB limit  #TODO make it configurable
        raise HTTPException(
            status_code=fapi_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds maximum allowed size (5000MB)"
        )

    # Read file data
    try:
        file_data = await file.read()
    except Exception as e:
        _logger.exception(f"Failed to read file: {str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file"
        )

    # Create validation config based on media type
    validation_config = FileValidationConfig(
        max_size=5000 * 1024 * 1024,  # 5000MB
        sanitize_filename=True,
        validate_content_headers=True,
        strict_mime_validation=True
    )

    # Set allowed extensions and MIME types based on media type
    if media_type.lower() == 'image':
        validation_config.allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'}
        validation_config.allowed_mime_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff'}
    elif media_type.lower() == 'video':
        validation_config.allowed_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
        validation_config.allowed_mime_types = {'video/mp4', 'video/avi', 'video/quicktime', 'video/x-msvideo', 'video/webm'}
    elif media_type.lower() == 'audio':
        validation_config.allowed_extensions = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'}
        validation_config.allowed_mime_types = {'audio/mpeg', 'audio/wav', 'audio/flac', 'audio/aac', 'audio/ogg'}

    # Handle filename issues
    original_filename = file.filename or "uploaded_file"

    # Log original filename for debugging problematic uploads
    _logger.info(f"Uploading file: '{original_filename}' (size: {len(file_data)} bytes)")

    # Store using enhanced media manager
    try:
        media_file_id = await media_manager.store_file_chunked(
            session=session,
            observation_id=observation_id,
            file_data=file_data,
            original_filename=original_filename,
            media_type=media_type,
            validation_config=validation_config
        )
        
        # Get file info for response
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        result = await session.exec(media_query)
        media_file = result.first()

        # Trigger background thumbnail generation for images and videos
        if media_file and media_type.lower() in ['image', 'video']:
            background_tasks.add_task(generate_thumbnail_background, media_file_id)

        return {
            "message": "File uploaded successfully",
            "media_file_id": media_file_id,
            "original_filename": media_file.original_filename if media_file else None,
            "file_size": media_file.file_size if media_file else None,
            "mime_type": media_file.mime_type if media_file else None
        }
        
    except ValueError as e:
        _logger.exception(str(e))
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Server side error"  # TODO swap to real error message
        )
    except Exception as e:
        _logger.exception(f"Upload failed: {str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed"
        )


@router.post(MediaRoutes.LIST_ALL_ATTACHED_MEDIA.value, response_model=List[MediaFileInfo])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_media_for_observation(
    request: ListAttachedMediaRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    List all media files for a specific observation.
    Users can only list media from their assigned post.
    """

    # Get observation first and check access
    observation_query = select(Observation).where(
        Observation.id == request.observation_id,
        Observation.is_deleted == False
    )
    obs_result = await session.exec(observation_query)
    observation = obs_result.first()

    if not observation:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    # Check if user has access to this observation's post
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.LIST_ALL_ATTACHED_MEDIA.value}"
    )

    # Query media files for the observation
    media_query = select(MediaFile).where(MediaFile.observation_id == request.observation_id)
    media_result = await session.exec(media_query)
    media_files = media_result.all()

    # Get chunk information for each file
    media_info_list = []
    for media_file in media_files:
        chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file.id)
        chunks_result = await session.exec(chunks_query)
        chunks = chunks_result.all()

        expected_chunks = (media_file.file_size + 1048576 - 1) // 1048576  # 1MB chunks
        upload_complete = len(chunks) == expected_chunks

        # Set thumbnail_id if thumbnail exists
        thumbnail_id = media_file.id if media_file.thumbnail_path else None

        media_info_list.append(MediaFileInfo(
            id=media_file.id,
            original_filename=media_file.original_filename,
            stored_filename=media_file.stored_filename,
            file_size=media_file.file_size,
            mime_type=media_file.mime_type,
            media_type=media_file.media_type,
            width=media_file.width,
            height=media_file.height,
            uploaded_at=media_file.uploaded_at,
            upload_complete=upload_complete,
            total_chunks=expected_chunks,
            uploaded_chunks=len(chunks),
            thumbnail_id=thumbnail_id
        ))

    return media_info_list


# Initialize thumbnail generator
thumbnail_generator = ThumbnailGenerator(settings.storage.thumbnail)


async def generate_thumbnail_background(media_file_id: int):
    """Background task to generate thumbnail with proper session and memory management"""
    media_file = None
    thumbnail_path = None

    try:
        _logger.info(f"Starting thumbnail generation for media_file_id={media_file_id}")

        # Create a new session for this background task using context manager
        session_maker = await get_session_maker()
        async with session_maker() as session:
            # Get media file
            media_file = await session.get(MediaFile, media_file_id)
            if not media_file:
                _logger.warning(f"Media file {media_file_id} not found")
                return

            _logger.info(f"Generating thumbnail for {media_file.original_filename}")

            # Generate thumbnail
            thumbnail_path = await thumbnail_generator.generate_thumbnail(
                media_file,
                size="medium"
            )

            if thumbnail_path:
                _logger.info(f"Thumbnail generated successfully at {thumbnail_path}")
                media_file.thumbnail_path = thumbnail_path
                media_file.thumbnail_error = None
            else:
                _logger.warning(f"Thumbnail generation returned None for media_file_id={media_file_id}")
                media_file.thumbnail_error = "Generation failed"

            # Commit the changes
            _logger.info(f"Committing thumbnail path to database for media_file_id={media_file_id}")
            await session.commit()
            _logger.info(f"Thumbnail path committed successfully for media_file_id={media_file_id}")

    except Exception as e:
        _logger.exception(f"Thumbnail generation failed for {media_file_id}: {e}")

        # Try to save generic error to database (don't expose error details)
        try:
            session_maker = await get_session_maker()
            async with session_maker() as error_session:
                media_file = await error_session.get(MediaFile, media_file_id)
                if media_file:
                    media_file.thumbnail_error = "Generation failed"
                    await error_session.commit()
                    _logger.info(f"Error status saved to database for media_file_id={media_file_id}")
        except Exception as commit_error:
            _logger.exception(f"Failed to save thumbnail error status: {commit_error}")

    finally:
        # Explicit cleanup to free memory
        del media_file
        del thumbnail_path

        # Force garbage collection for large image data
        gc.collect()
        _logger.info(f"Thumbnail generation task completed for media_file_id={media_file_id}")


@router.get(MediaRoutes.MEDIA_THUMBNAIL.value)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def get_media_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Serve thumbnail. If not generated, return 202 and queue generation.
    Check post access via AccessControlService.
    """
    # Get media file
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post
    observation = await get_observation_from_media(session, media_file)
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.MEDIA_THUMBNAIL.value}"
    )

    # Check thumbnail status
    if media_file.thumbnail_path:
        # Thumbnail exists, serve it
        thumb_path = Path(media_file.thumbnail_path)

        if thumb_path.exists():
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "ETag": f'"{media_file.file_hash}"'
                }
            )
        else:
            # Thumbnail path set but file missing, regenerate
            media_file.thumbnail_path = None
            media_file.thumbnail_error = "Thumbnail file missing"
            await session.commit()

    # Check if there was an error
    if media_file.thumbnail_error:
        # Return placeholder or error
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Thumbnail generation error: {media_file.thumbnail_error}"
        )

    # Thumbnail not generated yet, queue generation
    background_tasks.add_task(generate_thumbnail_background, media_id)

    raise HTTPException(
        status_code=fapi_status.HTTP_202_ACCEPTED,
        detail="Thumbnail generation in progress"
    )


@router.post(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE.value)
@PermissionService.require_permission(Permissions.IS_ADMIN.value)
async def regenerate_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """Force regenerate thumbnail (admin only)"""
    # Get media file
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Clear existing thumbnail
    if media_file.thumbnail_path:
        thumb_path = Path(media_file.thumbnail_path)
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception as e:
                _logger.warning(f"Failed to delete old thumbnail: {e}")

    media_file.thumbnail_path = None
    media_file.thumbnail_error = None
    await session.commit()

    # Queue regeneration
    background_tasks.add_task(generate_thumbnail_background, media_id)

    return {"message": "Thumbnail regeneration queued"}


# @router.get(MediaRoutes.OBSERVATION_THUMBNAILS.value, response_model=List[ThumbnailInfo])
# @PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
# async def list_observation_thumbnails(
#     observation_id: int,
#     size: str = "medium",
#     session: AsyncSession = Depends(get_session),
#     current_user: User = Depends(get_current_user)
# ):
#     """Get thumbnail URLs for all media in observation"""
#     # Get observation first and check access
#     observation_query = select(Observation).where(
#         Observation.id == observation_id,
#         Observation.is_deleted == False
#     )
#     obs_result = await session.exec(observation_query)
#     observation = obs_result.first()
#
#     if not observation:
#         raise HTTPException(
#             status_code=fapi_status.HTTP_404_NOT_FOUND,
#             detail="Observation not found"
#         )
#
#     # Check if user has access to this observation's post
#     await AccessControlService.check_post_membership(
#         current_user,
#         observation.post_id,
#         session,
#         f"{Permissions.OBSERVATION_VIEW.name} for {MediaRoutes.OBSERVATION_THUMBNAILS.value}"
#     )
#
#     # Query media files for the observation
#     media_query = select(MediaFile).where(MediaFile.observation_id == observation_id)
#     media_result = await session.exec(media_query)
#     media_files = media_result.all()
#
#     # Build thumbnail info list
#     thumbnail_list = []
#     for media_file in media_files:
#         thumbnail_ready = media_file.thumbnail_path is not None
#         thumbnail_url = None
#
#         if thumbnail_ready:
#             thumb_path = Path(media_file.thumbnail_path)
#             if thumb_path.exists():
#                 # Generate URL for thumbnail using route enum
#                 thumbnail_url = f"/api/v1{MediaRoutes.BASE.value}{MediaRoutes.MEDIA_THUMBNAIL.value.replace('{media_id}', str(media_file.id))}?size={size}"
#
#         thumbnail_list.append(ThumbnailInfo(
#             media_id=media_file.id,
#             thumbnail_url=thumbnail_url,
#             thumbnail_ready=thumbnail_ready,
#             error=media_file.thumbnail_error
#         ))
#
#     return thumbnail_list