from typing import List, Optional
from pathlib import Path
import gc
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, UploadFile, File, Form, BackgroundTasks, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.orm import selectinload
from sqlmodel import select, desc
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session, get_session_maker
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.user import User
from app.backend.services.auth_context import AuthContext
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.services.permissions import require
from app.backend.services.access_control import AccessControlService
from app.backend.services.media_manager import MediaManager, FileValidationConfig
from app.backend.enums.general import Permissions, MediaType, ObservationStatus
from app.backend.enums.routes import MediaRoutes, SystemRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.config import settings
from app.backend.routes.models.request.media_request import ListAttachedMediaRequest
from app.backend.routes.models.response.media_response import MediaFileInfo
from app.backend.filters.params.media_params import MediaFilterParams
from app.backend.filters.media_filter import MediaFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo
from app.backend.routes.helpers.media_helper import (
    get_observation_from_media,
    stream_media_file,
    get_x_accel_redirect_path,
    get_download_accel_redirect_path
)
from app.backend.services.thumbnail_generator import ThumbnailGenerator

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)
_logger = get_api_logger()


@router.get(MediaRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
async def get_media_filter_schema(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Available filter params and sortable fields for media files."""
    current_user = auth.user
    user_is_admin = auth.is_admin
    return EntitySchemaInfo(**MediaFilterService.get_schema(is_admin=user_is_admin))


@router.get(MediaRoutes.MEDIA_INFO.value, response_model=MediaFileInfo)
async def get_media_info(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """
    Get information about a media file.
    Users can only access media from their assigned post.
    """
    current_user = auth.user

    media_query = select(MediaFile).options(selectinload(MediaFile.user)).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Get observation and check post access (handles admin bypass)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media")

    return MediaFileInfo.model_validate(media_file)


@router.get(MediaRoutes.MEDIA_DOWNLOAD.value)
async def download_media_file(
    media_id: int,
    validate_integrity: bool = True,
    stream: bool = False,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """
    Download a complete media file with integrity validation and optional streaming.
    Users can only download media from their assigned post.
    """
    current_user = auth.user

    # Get media file info first
    media_query = select(MediaFile).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post (handles admin bypass)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media")

    # --- Nginx X-Accel-Redirect download (offloads file serving to Nginx) ---
    if settings.storage.enable_nginx:
        try:
            accel_path = get_download_accel_redirect_path(media_file, media_manager)
        except HTTPException:
            raise
        except Exception as e:
            _logger.error(f"Failed to build X-Accel-Redirect path for media {media_id}: {e}")
            raise HTTPException(
                status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to prepare download"
            )

        return Response(
            content=b"",
            status_code=200,
            headers={
                "X-Accel-Redirect": accel_path,
                "Content-Type": media_file.mime_type,
                "Content-Disposition": f"attachment; filename={media_file.original_filename}",
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=3600",
                "ETag": f'"{media_file.file_hash}"',
                "X-Accel-Buffering": "yes"
            }
        )

    # --- Direct download (fallback when not behind Nginx) ---

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


@router.get(MediaRoutes.MEDIA_STREAM.value)
async def stream_media_file_nginx(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """
    Stream media file using Nginx X-Accel-Redirect for optimal performance.
    Offloads file serving to Nginx, preventing Python worker blocking.
    Only works for video files with symlinks created.
    """
    current_user = auth.user

    # Get media file info first
    media_query = select(MediaFile).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post (handles admin bypass)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media")

    # Check if file is a video
    if media_file.media_type.lower() != 'video':
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Streaming is only available for video files"
        )

    # Check if Nginx streaming is enabled
    if not media_manager.enable_nginx:
        raise HTTPException(
            status_code=fapi_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Nginx streaming is not enabled"
        )

    # Get X-Accel-Redirect path
    try:
        accel_path = get_x_accel_redirect_path(media_file, media_manager)
    except HTTPException as e:
        _logger.error(f"Failed to get X-Accel-Redirect path for media {media_id}: {e.detail}")
        raise

    # Return empty response with X-Accel-Redirect header
    # Nginx will intercept and serve the file
    return Response(
        content=b"",
        status_code=200,
        headers={
            "X-Accel-Redirect": accel_path,
            "Content-Type": media_file.mime_type,
            "Content-Disposition": f"inline; filename={media_file.original_filename}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
            "ETag": f'"{media_file.file_hash}"',
            "X-Accel-Buffering": "yes"
        }
    )


@router.get(SystemRoutes.ROOT.value, response_model=List[MediaFileInfo])
async def list_media_files(
    filters: MediaFilterParams = Depends(),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """List media files with filters"""
    current_user = auth.user

    # Get post filter based on user access (handles admin bypass)
    query = select(MediaFile).where(MediaFile.is_deleted == False)
    query = MediaFilterService.apply_filters(query, filters, AccessControlService.post_scope(auth))
    result = await session.exec(query)
    media_files = result.all()

    return [MediaFileInfo.model_validate(mf) for mf in media_files]


@router.delete(MediaRoutes.MEDIA_BY_ID.value)
async def delete_media_file(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_DELETE]))
):
    """
    Delete a media file.
    Admins can delete any file.
    Regular users can only delete files they uploaded/created within their post.
    Implements soft delete (unlinks from observation but keeps file).
    """
    current_user = auth.user

    media_query = select(MediaFile).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Post scope, then ownership (media.manage_any overrides ownership and the active-observation rule)
    observation = None
    if media_file.observation_id:
        observation = await get_observation_from_media(session, media_file)
        AccessControlService.check_post_write(auth, observation.post_id, "media")
    AccessControlService.check_media_ownership(auth, media_file, observation)

    # Store old values for auditing
    old_values = media_file.model_dump()

    # Soft delete: mark as deleted and unlink from observation
    media_file.is_deleted = True
    media_file.observation_id = None
    
    # Old implementation with FS deletion (kept but not executed)
    if False:
        # Delete Nginx symlink for video files before deleting the media file
        if media_file.media_type.lower() == 'video':
            symlink_deleted = await media_manager.delete_nginx_symlink(media_file)
            if symlink_deleted:
                _logger.info(f"Nginx symlink deleted for video file {media_id}")
            else:
                _logger.debug(f"No symlink found or failed to delete for video file {media_id}")

        # Delete the filesystem file
        try:
            file_path = media_file.resolve_path(Path(settings.storage.upload_path))
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            _logger.warning(f"Failed to delete file {media_file.file_path}: {e}")

        # Try to remove empty directories
        try:
            file_path = media_file.resolve_path(Path(settings.storage.upload_path))
            parent_dir = file_path.parent
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
        except Exception as e:
            _logger.warning(f"Failed to cleanup directories: {e}")

        # Delete media file record
        await session.delete(media_file)
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="media",
        resource_id=media_id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        details={"message": f"User {current_user.username} soft-deleted media file '{old_values.get('original_filename')}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()

    return {"message": "Media file deleted successfully (soft-delete)"}


# Initialize media manager instance
media_manager = MediaManager(
    use_filesystem_cache=True,
    auto_cleanup=True,
    enable_deduplication=True,
    storage_path=getattr(settings.storage, 'upload_path', 'uploads'),
    temp_path=getattr(settings.storage, 'temp_path', 'temp'),
    nginx_symlink_path=getattr(settings.storage, 'nginx_symlink_path', None),
    enable_nginx=getattr(settings.storage, 'enable_nginx', False)
)


@router.get(MediaRoutes.STORAGE_STATISTICS.value)
async def get_storage_statistics(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW], admin=True))
):
    """Get storage statistics and health information"""

    stats = await media_manager.get_storage_statistics(session)
    return stats


@router.post(MediaRoutes.UPLOAD.value)
async def upload_complete_file(
    request: Request,
    observation_id: int = Form(...),
    media_type: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Upload a complete file with comprehensive validation and security checks.
    Users can only upload files for observations from their assigned post.
    """
    current_user = auth.user

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

    # Check if user has access to this observation's post (handles admin bypass)
    AccessControlService.check_post_write(auth, observation_result.post_id, "media")

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

    # Store using media manager
    try:
        media_file_id = await media_manager.store_file(
            session=session,
            observation_id=observation_id,
            file_data=file_data,
            original_filename=original_filename,
            media_type=media_type,
            user_id=current_user.id,
            validation_config=validation_config
        )

        # Get file info for response
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        result = await session.exec(media_query)
        media_file = result.first()

        # Create Nginx symlink for video files
        if media_file and media_type.lower() == 'video':
            symlink_created = await media_manager.create_nginx_symlink(media_file)
            if symlink_created:
                _logger.info(f"Nginx symlink created for video file {media_file_id}")
            else:
                _logger.warning(f"Failed to create Nginx symlink for video file {media_file_id}")

        # Trigger background thumbnail generation for images and videos
        # Skip when external_thumbnail_generation is enabled --
        # the media-watcher systemd service handles it in a separate process.
        if (
            media_file
            and media_type.lower() in ['image', 'video']
            and not settings.storage.external_thumbnail_generation
        ):
            background_tasks.add_task(generate_thumbnail_background, media_file_id)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="media",
            resource_id=media_file_id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            new_values=media_file.model_dump() if media_file else None,
            details={"message": f"User {current_user.username} uploaded media file '{original_filename}' for observation #{observation_id}"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

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


@router.post(MediaRoutes.NGINX_UPLOAD.value)
async def nginx_upload_file(
    request: Request,
    observation_id: int = Query(..., description="Observation ID to attach file to"),
    media_type: str = Query(..., description="Media type: image, video, or audio"),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Upload a file via Nginx offloading (client_body_in_file_only).

    Nginx receives the raw file body, writes it to a temp file on disk, and
    forwards the temp file path to FastAPI via the X-File-Name header.
    FastAPI validates the file, moves it to storage, and cleans up the temp file.
    The file is never loaded entirely into memory.

    Client sends raw binary body (not multipart form) with metadata in query params
    and the original filename in the X-Original-Filename header.
    """
    current_user = auth.user
    if not settings.storage.enable_nginx:
        raise HTTPException(
            status_code=fapi_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Nginx upload offloading is not enabled"
        )

    # Read temp file path from Nginx header
    temp_file_path = request.headers.get("X-File-Name")
    if not temp_file_path:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Missing X-File-Name header (Nginx temp file path)"
        )

    original_filename = request.headers.get("X-Original-Filename", "uploaded_file")

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

    # Check post membership (handles admin bypass)
    AccessControlService.check_post_write(auth, observation_result.post_id, "media")

    # Build validation config based on media type
    max_size_bytes = settings.storage.max_file_size_gb * 1024 * 1024 * 1024
    validation_config = FileValidationConfig(
        max_size=max_size_bytes,
        sanitize_filename=True,
        validate_content_headers=True,
        strict_mime_validation=True
    )

    if media_type.lower() == 'image':
        validation_config.allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'}
        validation_config.allowed_mime_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff'}
    elif media_type.lower() == 'video':
        validation_config.allowed_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
        validation_config.allowed_mime_types = {'video/mp4', 'video/avi', 'video/quicktime', 'video/x-msvideo', 'video/webm'}
    elif media_type.lower() == 'audio':
        validation_config.allowed_extensions = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'}
        validation_config.allowed_mime_types = {'audio/mpeg', 'audio/wav', 'audio/flac', 'audio/aac', 'audio/ogg'}

    try:
        media_file_id = await media_manager.store_file_from_path(
            session=session,
            observation_id=observation_id,
            source_path=temp_file_path,
            original_filename=original_filename,
            media_type=media_type,
            user_id=current_user.id,
            validation_config=validation_config
        )

        # Get file info for response
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        result = await session.exec(media_query)
        media_file = result.first()

        # Create Nginx symlink for video files
        if media_file and media_type.lower() == 'video':
            symlink_created = await media_manager.create_nginx_symlink(media_file)
            if symlink_created:
                _logger.info(f"Nginx symlink created for video file {media_file_id}")
            else:
                _logger.warning(f"Failed to create Nginx symlink for video file {media_file_id}")

        # Queue background thumbnail generation
        # Skip when external_thumbnail_generation is enabled --
        # the media-watcher systemd service handles it in a separate process.
        if (
            media_file
            and media_type.lower() in ['image', 'video']
            and not settings.storage.external_thumbnail_generation
        ):
            background_tasks.add_task(generate_thumbnail_background, media_file_id)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="media",
            resource_id=media_file_id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            new_values=media_file.model_dump() if media_file else None,
            details={"message": f"User {current_user.username} uploaded media file '{original_filename}' (nginx offload) for observation #{observation_id}"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return {
            "message": "File uploaded successfully (nginx offload)",
            "media_file_id": media_file_id,
            "original_filename": media_file.original_filename if media_file else None,
            "file_size": media_file.file_size if media_file else None,
            "mime_type": media_file.mime_type if media_file else None
        }

    except ValueError as e:
        _logger.exception(str(e))
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Server side error"
        )
    except Exception as e:
        _logger.exception(f"Nginx upload failed: {str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed"
        )


@router.post(MediaRoutes.LIST_ALL_ATTACHED_MEDIA.value, response_model=List[MediaFileInfo])
async def list_media_for_observation(
    request: ListAttachedMediaRequest,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """
    List all media files for a specific observation.
    Users can only list media from their assigned post.
    """
    current_user = auth.user

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

    # Check if user has access to this observation's post (handles admin bypass)
    AccessControlService.check_post_view(auth, observation.post_id, "observation media")

    # Query media files for the observation
    media_query = select(MediaFile).options(selectinload(MediaFile.user)).where(
        MediaFile.observation_id == request.observation_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_files = media_result.all()

    return [MediaFileInfo.model_validate(media_file) for media_file in media_files]


# Initialize thumbnail generator
thumbnail_generator = ThumbnailGenerator(settings.storage.thumbnail, Path(settings.storage.upload_path))


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

            # Skip if the external worker already finished while this task was queued
            if media_file.thumbnail_path:
                thumb_path = media_file.resolve_thumbnail_path(Path(settings.storage.upload_path))
                if thumb_path and thumb_path.exists():
                    _logger.info(f"Thumbnail already exists for media_file_id={media_file_id}, skipping")
                    return

            _logger.info(f"Generating thumbnail for {media_file.original_filename} (type: {media_file.media_type})")

            # Check for unsupported media types before calling generator
            if media_file.media_type.lower() not in ['image', 'video']:
                _logger.info(f"Skipping thumbnail for unsupported media type: {media_file.media_type}")
                media_file.thumbnail_error = f"Unsupported media type: {media_file.media_type}"
            else:
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
async def get_media_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW])),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Serve thumbnail. If not generated, return 202 and queue generation.
    Check post access via AccessControlService.
    """
    current_user = auth.user
    # Get media file
    media_query = select(MediaFile).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Check if user has access to this media's observation post (handles admin bypass)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media thumbnail")

    # Check thumbnail status
    if media_file.thumbnail_path:
        # Thumbnail exists, serve it
        thumb_path = media_file.resolve_thumbnail_path(Path(settings.storage.upload_path))

        if thumb_path.exists():
            if settings.storage.enable_nginx:
                upload_root = Path(settings.storage.upload_path).resolve()
                relative = thumb_path.resolve().relative_to(upload_root)
                prefix = settings.storage.nginx_internal_uploads_prefix
                return Response(
                    headers={
                        "X-Accel-Redirect": f"{prefix}/{relative}",
                        "Content-Type": "image/jpeg",
                        "Cache-Control": "public, max-age=3600",
                        "ETag": f'"{media_file.file_hash}"'
                    }
                )
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "ETag": f'"{media_file.file_hash}"'
                }
            )
        else:
            # Thumbnail path set but file missing — clear state and allow regeneration
            media_file.thumbnail_path = None
            media_file.thumbnail_error = None
            await session.commit()

    if media_file.thumbnail_error:
        # Unsupported type (e.g. audio) — will never succeed, tell the client
        if media_file.media_type.lower() not in ['image', 'video']:
            raise HTTPException(
                status_code=fapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Thumbnails are not supported for this media type"
            )
        # Previous failure on image/video — clear error and force internal retry.
        # The external watcher (inotify) only fires on new writes, so it will
        # never re-trigger for already-existing files. Use the internal task
        # unconditionally here to guarantee a retry actually happens.
        media_file.thumbnail_error = None
        await session.commit()
        background_tasks.add_task(generate_thumbnail_background, media_id)
        return JSONResponse(
            status_code=202,
            content={"detail": "Thumbnail generation in progress"}
        )

    # Always queue the internal task as a guaranteed fallback. The external
    # watcher only fires on new filesystem events so it misses files uploaded
    # while it was down or files that already existed. If the external worker
    # finishes first, generate_thumbnail_background detects the existing
    # thumbnail on disk and exits immediately without any CPU work.
    background_tasks.add_task(generate_thumbnail_background, media_id)

    return JSONResponse(
        status_code=202,
        content={"detail": "Thumbnail generation in progress"}
    )


@router.post(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE.value)
async def regenerate_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_UPDATE], admin=True)),
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
        thumb_path = media_file.resolve_thumbnail_path(Path(settings.storage.upload_path))
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
