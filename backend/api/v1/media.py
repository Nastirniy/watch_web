"""
Media file API routes for file uploads, downloads, and management.
Handles chunked uploads, file processing, and media metadata.
"""

import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status
)
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from backend.core.config.config import settings
from backend.core.exceptions import (
    FileProcessingException,
    ResourceNotFoundException,
    ValidationException,
)
from backend.core.permissions import SystemPermissions
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.media import media_file as media_crud, file_chunk as chunk_crud
from backend.dependencies import DatabaseDep, CurrentUser, require_permissions
from backend.models.audit import AuditAction, AuditSeverity, ResourceType
from backend.models.media import MediaType
from backend.schemas.base import (
    BulkOperationResponse,
    MessageResponse,
    PaginatedResponse,
    create_paginated_response,
)
from backend.schemas.media import (
    BulkMediaOperation,
    BulkMediaResponse,
    ChunkedUploadSession,
    ChunkUploadRequest,
    ChunkUploadResponse,
    MediaDownloadInfo,
    MediaFile,
    MediaFileCreate,
    MediaFileSearchFilters,
    MediaFileSummary,
    MediaFileUpdate,
    MediaProcessingJob,
    MediaStatistics,
    MediaUploadPolicy,
    StorageCleanupReport,
    ThumbnailRequest,
)

router = APIRouter()


def _determine_media_type(mime_type: str) -> MediaType:
    """Determine media type from MIME type."""
    if mime_type.startswith("image/"):
        return MediaType.IMAGE
    elif mime_type.startswith("video/"):
        return MediaType.VIDEO
    elif mime_type.startswith("audio/"):
        return MediaType.AUDIO
    else:
        return MediaType.DOCUMENT


def _validate_file_upload(file: UploadFile) -> None:
    """Validate uploaded file."""
    # Check file size
    if hasattr(file, 'size') and file.size and file.size > settings.MAX_FILE_SIZE:
        raise ValidationException(
            f"File size {file.size} exceeds maximum allowed size {settings.MAX_FILE_SIZE}",
            field="file"
        )

    # Check MIME type
    if file.content_type not in settings.ALLOWED_MEDIA_TYPES:
        raise ValidationException(
            f"File type {file.content_type} is not allowed",
            field="content_type"
        )


async def _save_uploaded_file(file: UploadFile, stored_filename: str) -> tuple[str, bytes]:
    """Save uploaded file and return path and content."""
    # Create upload directory if it doesn't exist
    upload_dir = Path(settings.UPLOAD_PATH)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Generate file path
    file_path = upload_dir / stored_filename

    # Read file content
    content = await file.read()

    # Save file
    with open(file_path, "wb") as f:
        f.write(content)

    return str(file_path), content


@router.post("/upload", response_model=MediaFile, status_code=status.HTTP_201_CREATED)
async def upload_media_file(
        observation_id: int = Form(..., description="Observation ID to attach file to"),
        file: UploadFile = File(..., description="File to upload"),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_CREATE)),
) -> MediaFile:
    """
    Upload a media file.

    Args:
        observation_id: Observation ID to attach file to
        file: File to upload
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created media file record
    """
    try:
        # Validate file
        _validate_file_upload(file)

        # Generate stored filename
        file_extension = Path(file.filename or "").suffix
        stored_filename = f"{uuid.uuid4()}{file_extension}"

        # Save file
        file_path, content = await _save_uploaded_file(file, stored_filename)

        # Determine media type
        media_type = _determine_media_type(file.content_type or "")

        # Create media file record
        media_file = await media_crud.create_with_metadata(
            db=db,
            observation_id=observation_id,
            original_filename=file.filename or "unknown",
            stored_filename=stored_filename,
            file_path=file_path,
            file_data=content,
            mime_type=file.content_type or "application/octet-stream",
            media_type=media_type,
            upload_ip=getattr(current_user, "last_ip", None),
            upload_user_agent=getattr(current_user, "last_user_agent", None),
        )

        # Log upload
        await audit_crud.create_log(
            db=db,
            action=AuditAction.FILE_UPLOAD,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_id=media_file.id,
            resource_name=media_file.original_filename,
            new_values={
                "observation_id": observation_id,
                "file_size": media_file.file_size,
                "media_type": media_type.value,
            },
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Media file {media_file.original_filename} uploaded by {current_user.username}")
        return media_file

    except (ValidationException, FileProcessingException):
        raise
    except Exception as e:
        logger.error(f"Error uploading media file: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload file"
        )


@router.post("/upload/chunked/start", response_model=ChunkedUploadSession)
async def start_chunked_upload(
        observation_id: int = Form(...),
        filename: str = Form(...),
        file_size: int = Form(...),
        chunk_size: int = Form(default=settings.CHUNK_SIZE),
        mime_type: str = Form(...),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_CREATE)),
) -> ChunkedUploadSession:
    """
    Start a chunked file upload session.

    Args:
        observation_id: Observation ID
        filename: Original filename
        file_size: Total file size
        chunk_size: Size of each chunk
        mime_type: File MIME type
        db: Database session
        current_user: Current authenticated user

    Returns:
        Upload session information
    """
    try:
        # Validate parameters
        if file_size > settings.MAX_FILE_SIZE:
            raise ValidationException(f"File size exceeds maximum allowed size")

        if mime_type not in settings.ALLOWED_MEDIA_TYPES:
            raise ValidationException(f"File type {mime_type} is not allowed")

        # Calculate number of chunks
        total_chunks = (file_size + chunk_size - 1) // chunk_size

        # Generate session ID
        session_id = str(uuid.uuid4())

        # Determine media type
        media_type = _determine_media_type(mime_type)

        # Create session (in production, this would be stored in Redis or database)
        from datetime import datetime, timedelta

        session = ChunkedUploadSession(
            session_id=session_id,
            observation_id=observation_id,
            original_filename=filename,
            total_size=file_size,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            uploaded_chunks=[],
            missing_chunks=list(range(total_chunks)),
            mime_type=mime_type,
            media_type=media_type,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=24),
            is_complete=False,
        )

        logger.info(f"Started chunked upload session {session_id} for {filename}")
        return session

    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Error starting chunked upload: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start chunked upload"
        )


@router.post("/upload/chunked/chunk", response_model=ChunkUploadResponse)
async def upload_chunk(
        chunk_data: ChunkUploadRequest,
        chunk_file: UploadFile = File(...),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_CREATE)),
) -> ChunkUploadResponse:
    """
    Upload a file chunk.

    Args:
        chunk_data: Chunk upload request data
        chunk_file: Chunk file data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Chunk upload response
    """
    try:
        # Read chunk content
        content = await chunk_file.read()

        # Verify chunk hash
        import hashlib
        calculated_hash = hashlib.sha256(content).hexdigest()
        if calculated_hash != chunk_data.chunk_hash:
            raise ValidationException("Chunk hash verification failed")

        # Note: In a real implementation, you would retrieve the session info
        # from Redis or database and create a proper media file when complete

        # For now, create a dummy response
        chunk_response = ChunkUploadResponse(
            chunk_id=0,  # Would be actual chunk ID
            chunk_number=chunk_data.chunk_number,
            is_verified=True,
            session_complete=chunk_data.is_last_chunk,
            media_file_id=None,  # Would be set when session is complete
        )

        logger.debug(f"Uploaded chunk {chunk_data.chunk_number} for session {chunk_data.session_id}")
        return chunk_response

    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Error uploading chunk: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload chunk"
        )


@router.get("", response_model=PaginatedResponse[MediaFileSummary])
async def list_media_files(
        filters: MediaFileSearchFilters = Depends(),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> PaginatedResponse[MediaFileSummary]:
    """
    List media files with filtering and pagination.

    Args:
        filters: Search filters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of media files
    """
    try:
        # Build filter dictionary
        filter_dict = {}
        if filters.observation_ids:
            filter_dict["observation_id"] = {"in": filters.observation_ids}
        if filters.media_types:
            filter_dict["media_type"] = {"in": [mt.value for mt in filters.media_types]}
        if filters.mime_types:
            filter_dict["mime_type"] = {"in": filters.mime_types}
        if filters.file_size_min:
            filter_dict["file_size"] = {"min": filters.file_size_min}
        if filters.file_size_max:
            if "file_size" not in filter_dict:
                filter_dict["file_size"] = {}
            filter_dict["file_size"]["max"] = filters.file_size_max
        if filters.uploaded_after:
            filter_dict["uploaded_at"] = {"min": filters.uploaded_after}
        if filters.uploaded_before:
            if "uploaded_at" not in filter_dict:
                filter_dict["uploaded_at"] = {}
            filter_dict["uploaded_at"]["max"] = filters.uploaded_before
        if filters.is_processed is not None:
            filter_dict["is_processed"] = filters.is_processed

        # Get media files
        media_files = await media_crud.get_multi(
            db=db,
            skip=(filters.page - 1) * filters.per_page,
            limit=filters.per_page,
            filters=filter_dict,
            order_by=f"{'-' if filters.sort_order == 'desc' else ''}{filters.sort_by or 'uploaded_at'}",
        )

        # Get total count
        total = await media_crud.count(db, filter_dict)

        # Convert to summary format
        media_summaries = []
        for media_file in media_files:
            media_summaries.append(MediaFileSummary(
                id=media_file.id,
                observation_id=media_file.observation_id,
                original_filename=media_file.original_filename,
                media_type=media_file.media_type,
                file_size=media_file.file_size,
                mime_type=media_file.mime_type,
                uploaded_at=media_file.uploaded_at,
                is_processed=media_file.is_processed,
                has_thumbnail=bool(media_file.thumbnail_path),
                has_preview=bool(media_file.preview_path),
            ))

        return create_paginated_response(
            items=media_summaries,
            total=total,
            page=filters.page,
            per_page=filters.per_page,
        )

    except Exception as e:
        logger.error(f"Error listing media files: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve media files"
        )


@router.get("/{media_id}", response_model=MediaFile)
async def get_media_file(
        media_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> MediaFile:
    """
    Get media file by ID.

    Args:
        media_id: Media file ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Media file details
    """
    media_file = await media_crud.get(db, media_id)
    if not media_file:
        raise ResourceNotFoundException("Media file", media_id)

    return media_file


@router.get("/{media_id}/download")
async def download_media_file(
        media_id: int,
        inline: bool = Query(False, description="Display inline in browser"),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> FileResponse:
    """
    Download media file.

    Args:
        media_id: Media file ID
        inline: Whether to display inline
        db: Database session
        current_user: Current authenticated user

    Returns:
        File response
    """
    try:
        # Get media file
        media_file = await media_crud.get(db, media_id)
        if not media_file:
            raise ResourceNotFoundException("Media file", media_id)

        # Check if file exists
        if not os.path.exists(media_file.file_path):
            raise ResourceNotFoundException("Physical file", media_file.file_path)

        # Log download
        await audit_crud.create_log(
            db=db,
            action=AuditAction.FILE_DOWNLOAD,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_id=media_id,
            resource_name=media_file.original_filename,
            success=True,
            severity=AuditSeverity.LOW,
        )

        # Return file response
        disposition = "inline" if inline else "attachment"
        return FileResponse(
            path=media_file.file_path,
            filename=media_file.original_filename,
            media_type=media_file.mime_type,
            headers={"Content-Disposition": f"{disposition}; filename={media_file.original_filename}"}
        )

    except ResourceNotFoundException:
        raise
    except Exception as e:
        logger.error(f"Error downloading media file {media_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download file"
        )


@router.get("/{media_id}/info", response_model=MediaDownloadInfo)
async def get_download_info(
        media_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> MediaDownloadInfo:
    """
    Get download information for media file.

    Args:
        media_id: Media file ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Download information
    """
    media_file = await media_crud.get(db, media_id)
    if not media_file:
        raise ResourceNotFoundException("Media file", media_id)

    from datetime import datetime, timedelta

    return MediaDownloadInfo(
        media_file_id=media_id,
        download_url=f"/api/v1/media/{media_id}/download",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        content_type=media_file.mime_type,
        content_length=media_file.file_size,
        filename=media_file.original_filename,
        is_inline=media_file.is_image,
    )


@router.put("/{media_id}", response_model=MediaFile)
async def update_media_file(
        media_id: int,
        update_data: MediaFileUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_UPDATE)),
) -> MediaFile:
    """
    Update media file metadata.

    Args:
        media_id: Media file ID
        update_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated media file
    """
    # Get existing media file
    media_file = await media_crud.get(db, media_id)
    if not media_file:
        raise ResourceNotFoundException("Media file", media_id)

    try:
        # Update media file
        updated_media = await media_crud.update(
            db, media_file, update_data.model_dump(exclude_unset=True)
        )

        # Log update
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_id=media_id,
            resource_name=updated_media.original_filename,
            new_values=update_data.model_dump(exclude_unset=True),
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Media file {media_id} updated by {current_user.username}")
        return updated_media

    except Exception as e:
        logger.error(f"Error updating media file {media_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update media file"
        )


@router.delete("/{media_id}", response_model=MessageResponse)
async def delete_media_file(
        media_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_DELETE)),
) -> MessageResponse:
    """
    Delete media file.

    Args:
        media_id: Media file ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Deletion confirmation
    """
    # Get media file
    media_file = await media_crud.get(db, media_id)
    if not media_file:
        raise ResourceNotFoundException("Media file", media_id)

    try:
        # Delete physical file
        if os.path.exists(media_file.file_path):
            os.remove(media_file.file_path)

        # Delete thumbnail and preview if they exist
        if media_file.thumbnail_path and os.path.exists(media_file.thumbnail_path):
            os.remove(media_file.thumbnail_path)

        if media_file.preview_path and os.path.exists(media_file.preview_path):
            os.remove(media_file.preview_path)

        # Delete database record
        await media_crud.delete(db, media_id)

        # Log deletion
        await audit_crud.create_log(
            db=db,
            action=AuditAction.DELETE,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_id=media_id,
            resource_name=media_file.original_filename,
            old_values={"file_size": media_file.file_size, "media_type": media_file.media_type.value},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Media file {media_file.original_filename} deleted by {current_user.username}")

        return MessageResponse(message="Media file deleted successfully")

    except Exception as e:
        logger.error(f"Error deleting media file {media_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete media file"
        )


# Bulk Operations
@router.post("/bulk", response_model=BulkMediaResponse)
async def bulk_media_operation(
        operation: BulkMediaOperation,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_MANAGE)),
) -> BulkMediaResponse:
    """
    Perform bulk operation on media files.

    Args:
        operation: Bulk operation request
        db: Database session
        current_user: Current authenticated user

    Returns:
        Bulk operation result
    """
    import time
    start_time = time.time()

    try:
        total_requested = len(operation.media_file_ids)
        successful = 0
        failed = 0
        errors = []

        for media_id in operation.media_file_ids:
            try:
                if operation.operation == "delete":
                    media_file = await media_crud.get(db, media_id)
                    if media_file:
                        # Delete physical file and record
                        if os.path.exists(media_file.file_path):
                            os.remove(media_file.file_path)
                        await media_crud.delete(db, media_id)
                        successful += 1
                    else:
                        failed += 1
                        errors.append({"media_id": media_id, "error": "File not found"})

                elif operation.operation == "reprocess":
                    # Placeholder for reprocessing logic
                    successful += 1

                else:
                    failed += 1
                    errors.append({"media_id": media_id, "error": f"Unknown operation: {operation.operation}"})

            except Exception as e:
                failed += 1
                errors.append({"media_id": media_id, "error": str(e)})

        processing_time = time.time() - start_time

        # Log bulk operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.DELETE if operation.operation == "delete" else AuditAction.UPDATE,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_name="bulk_operation",
            new_values={
                "operation": operation.operation,
                "media_ids": operation.media_file_ids,
                "successful": successful,
                "failed": failed,
            },
            success=failed == 0,
            severity=AuditSeverity.MEDIUM,
        )

        return BulkMediaResponse(
            operation=operation.operation,
            total_requested=total_requested,
            successful=successful,
            failed=failed,
            errors=errors,
            processing_time_seconds=processing_time,
        )

    except Exception as e:
        logger.error(f"Error in bulk media operation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bulk operation failed"
        )


# Statistics and Maintenance
@router.get("/statistics", response_model=MediaStatistics)
async def get_media_statistics(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> MediaStatistics:
    """
    Get media file statistics.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Media statistics
    """
    try:
        stats = await media_crud.get_storage_statistics(db)

        return MediaStatistics(
            total_files=stats["total_files"],
            total_size_bytes=stats["total_size_bytes"],
            by_media_type={
                "image_count": stats.get("image_count", 0),
                "video_count": stats.get("video_count", 0),
                "audio_count": stats.get("audio_count", 0),
                "document_count": stats.get("document_count", 0),
                "image_size": stats.get("image_size", 0),
                "video_size": stats.get("video_size", 0),
                "audio_size": stats.get("audio_size", 0),
                "document_size": stats.get("document_size", 0),
            },
            by_mime_type={},  # Would need additional implementation
            processing_stats={},  # Would need additional implementation
            upload_stats={},  # Would need additional implementation
            storage_usage={},  # Would need additional implementation
            error_count=stats.get("error_count", 0),
            orphaned_count=0,  # Would need additional implementation
        )

    except Exception as e:
        logger.error(f"Error getting media statistics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve media statistics"
        )


@router.post("/maintenance/cleanup", response_model=StorageCleanupReport)
async def cleanup_storage(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_MANAGE)),
) -> StorageCleanupReport:
    """
    Clean up orphaned files and expired chunks.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Cleanup report
    """
    import time
    from datetime import datetime

    start_time = time.time()

    try:
        # Clean up orphaned files
        orphaned_cleaned = await media_crud.cleanup_orphaned_files(db)

        # Clean up expired chunks
        expired_chunks_cleaned = await chunk_crud.cleanup_incomplete_uploads(db)

        processing_time = time.time() - start_time

        # Calculate space freed (simplified)
        space_freed = orphaned_cleaned * 1024 * 1024  # Placeholder calculation

        # Log cleanup operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.MAINTENANCE,
            resource_type=ResourceType.MEDIA,
            user_id=current_user.id,
            resource_name="storage_cleanup",
            new_values={
                "orphaned_files": orphaned_cleaned,
                "expired_chunks": expired_chunks_cleaned,
                "space_freed_bytes": space_freed,
            },
            success=True,
            severity=AuditSeverity.LOW,
        )

        return StorageCleanupReport(
            cleanup_date=datetime.utcnow(),
            orphaned_files_removed=orphaned_cleaned,
            expired_chunks_removed=expired_chunks_cleaned,
            space_freed_bytes=space_freed,
            errors_encountered=0,
            processing_time_seconds=processing_time,
        )

    except Exception as e:
        logger.error(f"Error in storage cleanup: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Storage cleanup failed"
        )


@router.get("/policy", response_model=MediaUploadPolicy)
async def get_upload_policy(
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.MEDIA_READ)),
) -> MediaUploadPolicy:
    """
    Get media upload policy and restrictions.

    Args:
        current_user: Current authenticated user

    Returns:
        Upload policy information
    """
    return MediaUploadPolicy(
        max_file_size=settings.MAX_FILE_SIZE,
        allowed_mime_types=settings.ALLOWED_MEDIA_TYPES,
        allowed_extensions=[".jpg", ".jpeg", ".png", ".gif", ".mp4", ".avi", ".mp3", ".wav", ".pdf", ".txt"],
        chunk_size=settings.CHUNK_SIZE,
        max_chunks=1000,
        session_timeout_minutes=60,
        require_authentication=True,
        virus_scanning_enabled=False,
        auto_processing_enabled=True,
    )
