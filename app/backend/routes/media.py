from typing import List, Optional
from datetime import datetime, timezone
import hashlib
import asyncio
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import select, desc
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
import io

from app.backend.database import get_session
from app.backend.models.media import MediaFile, FileChunk
from app.backend.models.observation import Observation
from app.backend.models.user import User
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.services.media_manager import MediaManager, FileIntegrityCheck, FileValidationConfig
from app.backend.enums.general import Permissions, MediaType
from app.backend.enums.routes import MediaRoutes, SystemRoutes
from app.backend.utils.mime_detector import mime_detector
from app.backend.utils.logging_config import get_logger
from app.backend.config import settings

router = APIRouter()
logger = get_logger("media")


async def _assemble_chunks_into_file(session: AsyncSession, media_file_id: int):
    """Assemble all chunks into a single complete file"""
    from pathlib import Path
    
    # Get media file info
    media_query = select(MediaFile).where(MediaFile.id == media_file_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()
    
    if not media_file:
        logger.error(f"Media file {media_file_id} not found for assembly")
        return
    
    # Get all chunks ordered by chunk number
    chunks_query = select(FileChunk).where(
        FileChunk.media_file_id == media_file_id
    ).order_by(FileChunk.chunk_number)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()
    
    if not chunks:
        logger.error(f"No chunks found for media file {media_file_id}")
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
                    logger.error(f"Chunk file not found: {chunk.chunk_file_path}")
                    return
        
        # Update media file with complete file path
        media_file.file_path = str(complete_file_path)
        media_file.upload_complete = True
        
        logger.info(f"Successfully assembled {len(chunks)} chunks into {complete_file_path}")
        
    except Exception as e:
        logger.error(f"Failed to assemble chunks for media file {media_file_id}: {e}")


async def _cleanup_expired_upload_session(session: AsyncSession, media_file_id: int):
    """Clean up expired upload session - remove all chunks and reset media file"""
    from pathlib import Path
    
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
            logger.warning(f"Failed to delete expired chunk file {chunk.chunk_file_path}: {e}")
        
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
        logger.warning(f"Failed to cleanup expired chunk directory: {e}")
    
    # Reset media file session tracking
    media_file.upload_session_started = get_default_timezone_datetime()
    media_file.upload_last_activity = get_default_timezone_datetime()
    media_file.upload_complete = False
    
    logger.info(f"Cleaned up expired upload session for media file {media_file_id}")


class ChunkedUploadStart(BaseModel):
    observation_id: int
    original_filename: str
    file_size: int
    chunk_size: int
    total_chunks: int
    file_extension: str
    media_type: str


class ChunkedUploadResponse(BaseModel):
    upload_id: str
    media_file_id: int
    chunk_size: int
    total_chunks: int
    uploaded_chunks: List[int]


class ChunkUploadResponse(BaseModel):
    chunk_number: int
    uploaded: bool
    remaining_chunks: int
    upload_complete: bool


class MediaFileInfo(BaseModel):
    id: int
    original_filename: str
    stored_filename: str
    file_size: int
    mime_type: str
    media_type: str
    width: Optional[int]
    height: Optional[int]
    uploaded_at: datetime
    upload_complete: bool
    total_chunks: int
    uploaded_chunks: int


@router.post(MediaRoutes.UPLOAD_CHUNKED_START.path(), response_model=ChunkedUploadResponse)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def start_chunked_upload(
    upload_data: ChunkedUploadStart,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Start a chunked file upload session"""
    
    # Verify observation exists and user has access
    observation_query = select(Observation).where(
        Observation.id == upload_data.observation_id,
        Observation.is_deleted is False
    )
    observation = await session.exec(observation_query)
    observation_result = observation.first()
    
    if not observation_result:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )
    
    # Validate media type
    try:
        media_type_enum = MediaType(upload_data.media_type.lower())
    except ValueError:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid media type: {upload_data.media_type}"
        )
    
    # Generate unique stored filename
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_hash_base = f"{upload_data.original_filename}_{upload_data.file_size}_{timestamp}"
    file_hash = hashlib.sha256(file_hash_base.encode()).hexdigest()[:16]
    stored_filename = f"{file_hash}_{upload_data.original_filename}"
    
    # Create media file record
    media_file = MediaFile(
        observation_id=upload_data.observation_id,
        original_filename=upload_data.original_filename,
        stored_filename=stored_filename,
        file_path=f"{settings.storage.upload_path}/{observation_result.id}/{stored_filename}",
        file_size=upload_data.file_size,
        mime_type="application/octet-stream",  # Will be updated when first chunk arrives
        file_extension=upload_data.file_extension,
        media_type=media_type_enum.value,
        file_hash=file_hash
    )
    
    session.add(media_file)
    await session.commit()
    await session.refresh(media_file)
    
    upload_id = f"{media_file.id}_{file_hash}"
    
    return ChunkedUploadResponse(
        upload_id=upload_id,
        media_file_id=media_file.id,
        chunk_size=upload_data.chunk_size,
        total_chunks=upload_data.total_chunks,
        uploaded_chunks=[]
    )


@router.post(MediaRoutes.UPLOAD_CHUNKED_CHUNK.path(), response_model=ChunkUploadResponse)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_number: int = Form(...),
    chunk_file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Upload a single chunk of a file"""
    
    # Parse upload_id to get media_file_id
    try:
        media_file_id = int(upload_id.split('_')[0])
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Invalid upload_id format"
        )
    
    # Get media file
    media_query = select(MediaFile).where(MediaFile.id == media_file_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()
    
    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )
    
    # Check upload session timeout (30 minutes default)
    from datetime import timedelta
    current_time = get_default_timezone_datetime()
    timeout_duration = timedelta(minutes=settings.storage.upload_timeout_minutes)
    
    if current_time - media_file.upload_last_activity > timeout_duration:
        # Session expired, clean up chunks and restart
        await _cleanup_expired_upload_session(session, media_file_id)
        raise HTTPException(
            status_code=fapi_status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"Upload session expired after {settings.storage.upload_timeout_minutes} minutes. Please restart upload."
        )
    
    # Update last activity time
    media_file.upload_last_activity = current_time
    
    # Check if chunk already exists
    chunk_query = select(FileChunk).where(
        FileChunk.media_file_id == media_file_id,
        FileChunk.chunk_number == chunk_number
    )
    existing_chunk = await session.exec(chunk_query)
    if existing_chunk.first():
        # Chunk already uploaded, skip
        pass
    else:
        # Read and validate chunk data
        chunk_data = await chunk_file.read()
        chunk_size = len(chunk_data)
        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
        
        # Create uploads directory structure
        from pathlib import Path
        uploads_dir = Path(settings.storage.upload_path)
        observation_dir = uploads_dir / str(media_file.observation_id)
        chunks_dir = observation_dir / f"media_{media_file_id}"
        
        chunks_dir.mkdir(parents=True, exist_ok=True)
        
        # Save chunk to filesystem
        chunk_filename = f"chunk_{chunk_number:04d}.bin"
        chunk_file_path = chunks_dir / chunk_filename
        
        with open(chunk_file_path, "wb") as f:
            f.write(chunk_data)
        
        # Detect mime type from first chunk using advanced detection
        if chunk_number == 0:
            detected_mime, detection_method = mime_detector.detect_mime_type(
                chunk_data, 
                media_file.original_filename
            )
            
            media_file.mime_type = detected_mime
            
            # Log detection method for debugging
            logger.info(f"MIME type detected for {media_file.original_filename}: {detected_mime} (method: {detection_method})")
        
        # Create chunk record with file path
        file_chunk = FileChunk(
            media_file_id=media_file_id,
            chunk_number=chunk_number,
            chunk_size=chunk_size,
            chunk_hash=chunk_hash,
            chunk_file_path=str(chunk_file_path)
        )
        
        session.add(file_chunk)
    
    # Get all uploaded chunks
    all_chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file_id)
    all_chunks = await session.exec(all_chunks_query)
    uploaded_chunks = [chunk.chunk_number for chunk in all_chunks.all()]
    
    # Calculate expected total chunks based on file size
    expected_chunks = (media_file.file_size + 1048576 - 1) // 1048576  # 1MB chunks
    remaining_chunks = expected_chunks - len(uploaded_chunks)
    upload_complete = remaining_chunks == 0
    
    # If upload is complete, assemble the chunks into a single file
    if upload_complete:
        await _assemble_chunks_into_file(session, media_file_id)
    
    await session.commit()
    
    return ChunkUploadResponse(
        chunk_number=chunk_number,
        uploaded=True,
        remaining_chunks=remaining_chunks,
        upload_complete=upload_complete
    )


@router.get(MediaRoutes.MEDIA_INFO.path(), response_model=MediaFileInfo)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def get_media_info(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Get information about a media file and its upload status"""
    
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()
    
    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
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


@router.get(MediaRoutes.MEDIA_DOWNLOAD.path())
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def download_media_file(
    media_id: int,
    validate_integrity: bool = True,
    stream: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Download a complete media file with integrity validation and optional streaming"""

    # Get media file info first
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()

    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )

    # Handle streaming download for large files
    if stream and media_file.file_size > 10 * 1024 * 1024:  # 10MB threshold
        return await _stream_media_file(session, media_file, validate_integrity)
    
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

        from fastapi.responses import Response
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
        # Handle integrity validation failures
        if "integrity" in str(e).lower():
            raise HTTPException(
                status_code=fapi_status.HTTP_409_CONFLICT,
                detail=f"File integrity validation failed: {str(e)}"
            )
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Failed to download media file {media_id}: {e}")
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


async def _stream_media_file(session: AsyncSession, media_file: MediaFile, validate_integrity: bool = True):
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
            logger.error(f"Error streaming media file {media_file.id}: {e}")
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


@router.delete(MediaRoutes.MEDIA_BY_ID.path())
@PermissionService.require_permission(Permissions.OBSERVATION_DELETE.value)
async def delete_media_file(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Delete a media file and all its chunks"""
    
    media_query = select(MediaFile).where(MediaFile.id == media_id)
    media_result = await session.exec(media_query)
    media_file = media_result.first()
    
    if not media_file:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Media file not found"
        )
    
    # Delete all chunks first (due to foreign key constraint)
    chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_id)
    chunks_result = await session.exec(chunks_query)
    chunks = chunks_result.all()
    
    from pathlib import Path
    for chunk in chunks:
        # Delete chunk file from filesystem
        try:
            chunk_path = Path(chunk.chunk_file_path)
            if chunk_path.exists():
                chunk_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete chunk file {chunk.chunk_file_path}: {e}")
        
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
        logger.warning(f"Failed to cleanup directories: {e}")
    
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


@router.get("/integrity/{media_id}", response_model=FileIntegrityCheck)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def check_file_integrity(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Check file integrity and detect any issues"""
    
    integrity = await media_manager.check_file_integrity(session, media_id)
    return integrity


@router.post("/repair/{media_id}")
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def repair_file(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Attempt to repair a corrupted or incomplete file"""
    
    success = await media_manager.repair_file(session, media_id)
    
    if success:
        return {"message": f"File {media_id} repaired successfully"}
    else:
        raise HTTPException(
            status_code=fapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File could not be repaired"
        )


@router.post(MediaRoutes.MAINTENANCE_CLEANUP.path())
@PermissionService.require_permission(Permissions.ADMIN_FULL.value)
async def cleanup_orphaned_files(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Clean up orphaned files and database records"""
    
    db_cleaned, fs_cleaned = await media_manager.cleanup_orphaned_files(session)
    
    return {
        "message": "Cleanup completed",
        "database_records_cleaned": db_cleaned,
        "filesystem_files_cleaned": fs_cleaned
    }


@router.get(MediaRoutes.STATISTICS.path())
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def get_storage_statistics(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Get storage statistics and health information"""
    
    stats = await media_manager.get_storage_statistics(session)
    return stats


@router.post(MediaRoutes.UPLOAD.path())
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def upload_complete_file(
    observation_id: int = Form(...),
    media_type: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Upload a complete file with comprehensive validation and security checks"""
    
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
    
    # Validate file size before reading (basic check)
    if file.size and file.size > 500 * 1024 * 1024:  # 500MB limit
        raise HTTPException(
            status_code=fapi_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds maximum allowed size (500MB)"
        )

    # Read file data
    try:
        file_data = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {str(e)}"
        )

    # Create validation config based on media type
    validation_config = FileValidationConfig(
        max_size=500 * 1024 * 1024,  # 500MB
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

    # Store using enhanced media manager
    try:
        media_file_id = await media_manager.store_file_chunked(
            session=session,
            observation_id=observation_id,
            file_data=file_data,
            original_filename=file.filename or "uploaded_file",
            media_type=media_type,
            validation_config=validation_config
        )
        
        # Get file info for response
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        result = await session.exec(media_query)
        media_file = result.first()

        return {
            "message": "File uploaded successfully",
            "media_file_id": media_file_id,
            "original_filename": media_file.original_filename if media_file else None,
            "file_size": media_file.file_size if media_file else None,
            "mime_type": media_file.mime_type if media_file else None
        }
        
    except ValueError as e:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed: {str(e)}"
        )


@router.get("/observation/{observation_id}", response_model=List[MediaFileInfo])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_media_for_observation(
    observation_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """List all media files for a specific observation"""

    # Query media files for the observation
    media_query = select(MediaFile).where(MediaFile.observation_id == observation_id)
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