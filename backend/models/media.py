"""
Media file models for handling file uploads and storage.
Supports chunked file uploads and various media types.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship

from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.signal import Observation


class MediaType(str, Enum):
    """Media file type enumeration."""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class MediaFile(BaseModel, table=True):
    """Media file metadata and storage information."""

    __tablename__ = "media_files"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Associated observation
    observation_id: int = Field(foreign_key="observations.id", index=True, description="Related observation ID")

    # File metadata
    original_filename: str = Field(max_length=255, description="Original uploaded filename")
    stored_filename: str = Field(
        max_length=255,
        unique=True,
        description="Stored filename (UUID-based)"
    )
    file_path: str = Field(max_length=500, description="Full file storage path")

    # File properties
    file_size: int = Field(description="File size in bytes")
    mime_type: str = Field(max_length=100, index=True, description="MIME type")
    file_extension: str = Field(max_length=10, description="File extension")
    media_type: MediaType = Field(index=True, description="Media category")

    # File integrity
    file_hash: str = Field(max_length=64, index=True, description="SHA-256 file hash")
    checksum_algorithm: str = Field(default="sha256", description="Checksum algorithm used")

    # Media-specific metadata
    width: Optional[int] = Field(default=None, description="Image/video width in pixels")
    height: Optional[int] = Field(default=None, description="Image/video height in pixels")
    duration: Optional[float] = Field(default=None, description="Audio/video duration in seconds")
    bitrate: Optional[int] = Field(default=None, description="Audio/video bitrate")

    # Processing status
    is_processed: bool = Field(default=False, description="File processing completion status")
    processing_error: Optional[str] = Field(default=None, description="Processing error message")

    # Upload metadata
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, index=True, description="Upload timestamp")
    upload_ip: Optional[str] = Field(default=None, description="Uploader IP address")
    upload_user_agent: Optional[str] = Field(default=None, description="Uploader user agent")

    # Thumbnail and preview
    thumbnail_path: Optional[str] = Field(default=None, description="Thumbnail file path")
    preview_path: Optional[str] = Field(default=None, description="Preview file path")

    # Relationships
    observation: "Observation" = Relationship(back_populates="media_files")
    chunks: List["FileChunk"] = Relationship(back_populates="media_file", cascade_delete=True)

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
    def is_image(self) -> bool:
        """Check if file is an image."""
        return self.media_type == MediaType.IMAGE

    @property
    def is_video(self) -> bool:
        """Check if file is a video."""
        return self.media_type == MediaType.VIDEO

    @property
    def is_audio(self) -> bool:
        """Check if file is audio."""
        return self.media_type == MediaType.AUDIO

    @property
    def is_document(self) -> bool:
        """Check if file is a document."""
        return self.media_type == MediaType.DOCUMENT

    @property
    def has_chunks(self) -> bool:
        """Check if file was uploaded in chunks."""
        return len(self.chunks) > 0

    def get_download_url(self, base_url: str) -> str:
        """Generate download URL for the file."""
        return f"{base_url}/api/v1/media/{self.id}/download"


class FileChunk(BaseModel, table=True):
    """File chunk for large file uploads."""

    __tablename__ = "file_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Associated media file
    media_file_id: int = Field(foreign_key="media_files.id", index=True, description="Media file ID")

    # Chunk metadata
    chunk_number: int = Field(description="Chunk sequence number (0-based)")
    chunk_size: int = Field(description="Chunk size in bytes")
    chunk_hash: str = Field(max_length=64, description="Chunk SHA-256 hash")

    # Chunk data
    chunk_data: bytes = Field(description="Binary chunk data")

    # Upload tracking
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, description="Chunk upload timestamp")
    upload_order: Optional[int] = Field(default=None, description="Upload order (may differ from chunk_number)")

    # Verification
    is_verified: bool = Field(default=False, description="Chunk integrity verification status")
    verification_attempts: int = Field(default=0, description="Number of verification attempts")

    # Relationships
    media_file: MediaFile = Relationship(back_populates="chunks")

    class Config:
        """SQLModel configuration."""
        # Ensure unique constraint on media_file_id + chunk_number
        table_args = {"extend_existing": True}

    @property
    def chunk_size_formatted(self) -> str:
        """Get human-readable chunk size."""
        size = self.chunk_size
        units = ['B', 'KB', 'MB']
        unit_index = 0

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        return f"{size:.2f} {units[unit_index]}"

    def verify_chunk(self) -> bool:
        """Verify chunk integrity against stored hash."""
        import hashlib

        calculated_hash = hashlib.sha256(self.chunk_data).hexdigest()
        is_valid = calculated_hash == self.chunk_hash

        self.verification_attempts += 1
        self.is_verified = is_valid

        return is_valid
