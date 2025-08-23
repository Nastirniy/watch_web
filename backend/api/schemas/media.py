"""
Media file schemas for file uploads, downloads, and management.
Handles file metadata, chunked uploads, and media processing.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import Field, field_validator

from backend.database.models import MediaType
from backend.api.schemas.base import BaseSchema, TimestampSchema, SearchFilters


class MediaFileBase(BaseSchema):
    """Base media file schema."""

    observation_id: int = Field(description="Related observation ID")
    original_filename: str = Field(description="Original uploaded filename")
    mime_type: str = Field(description="MIME type")
    media_type: MediaType = Field(description="Media category")
    width: Optional[int] = Field(None, description="Image/video width in pixels")
    height: Optional[int] = Field(None, description="Image/video height in pixels")
    duration: Optional[float] = Field(None, description="Audio/video duration in seconds")
    bitrate: Optional[int] = Field(None, description="Audio/video bitrate")


class MediaFileCreate(MediaFileBase):
    """Schema for creating media files."""

    @field_validator('original_filename')
    @classmethod
    def validate_filename(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Filename cannot be empty")
        # Remove potentially dangerous characters
        dangerous_chars = ['<', '>', ':', '"', '|', '?', '*', '\\', '/']
        for char in dangerous_chars:
            if char in v:
                raise ValueError(f"Filename cannot contain '{char}'")
        return v.strip()

    @field_validator('width', 'height')
    @classmethod
    def validate_dimensions(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("Dimensions must be positive")
        return v

    @field_validator('duration')
    @classmethod
    def validate_duration(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("Duration must be positive")
        return v


class MediaFileUpdate(BaseSchema):
    """Schema for updating media files."""

    width: Optional[int] = Field(None, description="Image/video width")
    height: Optional[int] = Field(None, description="Image/video height")
    duration: Optional[float] = Field(None, description="Duration in seconds")
    bitrate: Optional[int] = Field(None, description="Audio/video bitrate")
    is_processed: Optional[bool] = Field(None, description="Processing completion status")
    processing_error: Optional[str] = Field(None, description="Processing error message")


class MediaFile(MediaFileBase, TimestampSchema):
    """Media file response schema."""

    id: int = Field(description="Media file ID")
    stored_filename: str = Field(description="Stored filename (UUID-based)")
    file_path: str = Field(description="Full file storage path")
    file_size: int = Field(description="File size in bytes")
    file_extension: str = Field(description="File extension")
    file_hash: str = Field(description="SHA-256 file hash")
    checksum_algorithm: str = Field(description="Checksum algorithm used")
    is_processed: bool = Field(description="File processing completion status")
    processing_error: Optional[str] = Field(None, description="Processing error message")
    uploaded_at: datetime = Field(description="Upload timestamp")
    upload_ip: Optional[str] = Field(None, description="Uploader IP address")
    upload_user_agent: Optional[str] = Field(None, description="Uploader user agent")
    thumbnail_path: Optional[str] = Field(None, description="Thumbnail file path")
    preview_path: Optional[str] = Field(None, description="Preview file path")
    chunk_count: Optional[int] = Field(None, description="Number of chunks (if chunked upload)")

    @property
    def file_size_formatted(self) -> str:
        """Get human-readable file size."""
        size = self.file_size
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        unit_index = 0

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        return f"{size:.2f} {units[unit_index]}"

    @property
    def duration_formatted(self) -> Optional[str]:
        """Get formatted duration string."""
        if self.duration is None:
            return None

        hours, remainder = divmod(int(self.duration), 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"


class MediaFileSummary(BaseSchema):
    """Simplified media file schema for listings."""

    id: int = Field(description="Media file ID")
    observation_id: int = Field(description="Observation ID")
    original_filename: str = Field(description="Original filename")
    media_type: MediaType = Field(description="Media category")
    file_size: int = Field(description="File size in bytes")
    mime_type: str = Field(description="MIME type")
    uploaded_at: datetime = Field(description="Upload timestamp")
    is_processed: bool = Field(description="Processing status")
    has_thumbnail: bool = Field(default=False, description="Has thumbnail")
    has_preview: bool = Field(default=False, description="Has preview")


class FileChunkInfo(BaseSchema):
    """File chunk information."""

    id: int = Field(description="Chunk ID")
    media_file_id: int = Field(description="Media file ID")
    chunk_number: int = Field(description="Chunk sequence number")
    chunk_size: int = Field(description="Chunk size in bytes")
    chunk_hash: str = Field(description="Chunk SHA-256 hash")
    uploaded_at: datetime = Field(description="Chunk upload timestamp")
    is_verified: bool = Field(description="Chunk integrity verification status")


class ChunkedUploadSession(BaseSchema):
    """Chunked upload session information."""

    session_id: str = Field(description="Upload session ID")
    observation_id: int = Field(description="Target observation ID")
    original_filename: str = Field(description="Original filename")
    total_size: int = Field(description="Total file size")
    total_chunks: int = Field(description="Total number of chunks")
    chunk_size: int = Field(description="Size of each chunk")
    uploaded_chunks: List[int] = Field(description="List of uploaded chunk numbers")
    missing_chunks: List[int] = Field(description="List of missing chunk numbers")
    mime_type: str = Field(description="MIME type")
    media_type: MediaType = Field(description="Media category")
    created_at: datetime = Field(description="Session creation timestamp")
    expires_at: datetime = Field(description="Session expiration timestamp")
    is_complete: bool = Field(description="Upload completion status")


class ChunkUploadRequest(BaseSchema):
    """Chunk upload request."""

    session_id: str = Field(description="Upload session ID")
    chunk_number: int = Field(description="Chunk sequence number")
    chunk_hash: str = Field(description="Expected chunk hash")
    is_last_chunk: bool = Field(default=False, description="Is this the last chunk")


class ChunkUploadResponse(BaseSchema):
    """Chunk upload response."""

    chunk_id: int = Field(description="Created chunk ID")
    chunk_number: int = Field(description="Chunk number")
    is_verified: bool = Field(description="Chunk verification status")
    session_complete: bool = Field(description="All chunks uploaded")
    media_file_id: Optional[int] = Field(None, description="Created media file ID (if complete)")


class MediaFileSearchFilters(SearchFilters):
    """Media file search filters."""

    observation_ids: Optional[List[int]] = Field(None, description="Filter by observation IDs")
    media_types: Optional[List[MediaType]] = Field(None, description="Filter by media types")
    mime_types: Optional[List[str]] = Field(None, description="Filter by MIME types")
    file_size_min: Optional[int] = Field(None, description="Minimum file size")
    file_size_max: Optional[int] = Field(None, description="Maximum file size")
    uploaded_after: Optional[datetime] = Field(None, description="Uploaded after date")
    uploaded_before: Optional[datetime] = Field(None, description="Uploaded before date")
    is_processed: Optional[bool] = Field(None, description="Filter by processing status")
    has_errors: Optional[bool] = Field(None, description="Filter by error status")


class MediaProcessingJob(BaseSchema):
    """Media processing job information."""

    id: int = Field(description="Job ID")
    media_file_id: int = Field(description="Media file ID")
    job_type: str = Field(description="Processing job type")
    status: str = Field(description="Job status")
    progress: int = Field(description="Processing progress (0-100)")
    started_at: Optional[datetime] = Field(None, description="Job start timestamp")
    completed_at: Optional[datetime] = Field(None, description="Job completion timestamp")
    error_message: Optional[str] = Field(None, description="Error message if failed")
    output_files: List[str] = Field(default_factory=list, description="Generated output files")


class ThumbnailRequest(BaseSchema):
    """Thumbnail generation request."""

    media_file_id: int = Field(description="Media file ID")
    width: int = Field(default=200, ge=50, le=1000, description="Thumbnail width")
    height: int = Field(default=200, ge=50, le=1000, description="Thumbnail height")
    quality: int = Field(default=85, ge=1, le=100, description="JPEG quality")
    format: str = Field(default="jpeg", description="Output format")


class MediaStatistics(BaseSchema):
    """Media file statistics."""

    total_files: int = Field(description="Total number of files")
    total_size_bytes: int = Field(description="Total storage size in bytes")
    by_media_type: dict = Field(default_factory=dict, description="Count and size by media type")
    by_mime_type: dict = Field(default_factory=dict, description="Count by MIME type")
    processing_stats: dict = Field(default_factory=dict, description="Processing statistics")
    upload_stats: dict = Field(default_factory=dict, description="Upload statistics")
    storage_usage: dict = Field(default_factory=dict, description="Storage usage breakdown")
    error_count: int = Field(description="Files with processing errors")
    orphaned_count: int = Field(description="Orphaned file records")


class StorageCleanupReport(BaseSchema):
    """Storage cleanup report."""

    cleanup_date: datetime = Field(description="Cleanup execution date")
    orphaned_files_removed: int = Field(description="Orphaned files removed")
    expired_chunks_removed: int = Field(description="Expired chunks removed")
    space_freed_bytes: int = Field(description="Storage space freed")
    errors_encountered: int = Field(description="Cleanup errors encountered")
    processing_time_seconds: float = Field(description="Cleanup processing time")


class BulkMediaOperation(BaseSchema):
    """Bulk media operation request."""

    media_file_ids: List[int] = Field(description="Media file IDs")
    operation: str = Field(description="Operation type (delete, reprocess, etc.)")
    parameters: Optional[dict] = Field(None, description="Operation parameters")


class BulkMediaResponse(BaseSchema):
    """Bulk media operation response."""

    operation: str = Field(description="Operation type")
    total_requested: int = Field(description="Total files requested")
    successful: int = Field(description="Successfully processed")
    failed: int = Field(description="Failed operations")
    errors: List[dict] = Field(default_factory=list, description="Error details")
    processing_time_seconds: float = Field(description="Total processing time")


class MediaDownloadInfo(BaseSchema):
    """Media download information."""

    media_file_id: int = Field(description="Media file ID")
    download_url: str = Field(description="Download URL")
    expires_at: datetime = Field(description="URL expiration time")
    content_type: str = Field(description="Content type")
    content_length: int = Field(description="Content length")
    filename: str = Field(description="Download filename")
    is_inline: bool = Field(default=False, description="Display inline in browser")


class MediaUploadPolicy(BaseSchema):
    """Media upload policy and restrictions."""

    max_file_size: int = Field(description="Maximum file size in bytes")
    allowed_mime_types: List[str] = Field(description="Allowed MIME types")
    allowed_extensions: List[str] = Field(description="Allowed file extensions")
    chunk_size: int = Field(description="Recommended chunk size for uploads")
    max_chunks: int = Field(description="Maximum number of chunks")
    session_timeout_minutes: int = Field(description="Upload session timeout")
    require_authentication: bool = Field(description="Authentication required")
    virus_scanning_enabled: bool = Field(description="Virus scanning enabled")
    auto_processing_enabled: bool = Field(description="Automatic processing enabled")
