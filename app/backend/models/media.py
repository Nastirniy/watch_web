from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from app.backend.utils.datetime_utils import get_default_timezone_datetime

if TYPE_CHECKING:
    from app.backend.models.observation import Observation


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    observation_id: int = Field(foreign_key="observations.id", index=True)

    original_filename: str = Field(max_length=255)
    stored_filename: str = Field(unique=True, max_length=255)
    file_path: str = Field(max_length=500)
    file_size: int
    mime_type: str = Field(max_length=100)
    file_extension: str = Field(max_length=10)

    media_type: str = Field(max_length=10, index=True)

    file_hash: str = Field(max_length=64, index=True)

    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)

    uploaded_at: datetime = Field(default_factory=get_default_timezone_datetime, index=True)
    upload_complete: bool = Field(default=False)
    upload_session_started: datetime = Field(default_factory=get_default_timezone_datetime, index=True)
    upload_last_activity: datetime = Field(default_factory=get_default_timezone_datetime, index=True)
    upload_timeout_minutes: int = Field(default=30)

    # Relationships
    observation: "Observation" = Relationship(back_populates="media_files")
    file_chunks: List["FileChunk"] = Relationship(back_populates="media_file", cascade_delete=True)


class FileChunk(SQLModel, table=True):
    __tablename__ = "file_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="media_files.id", index=True)
    chunk_number: int = Field(index=True)
    chunk_size: int
    chunk_hash: str = Field(max_length=64)
    chunk_file_path: str = Field(max_length=500)  # Path to chunk file on filesystem
    uploaded_at: datetime = Field(default_factory=get_default_timezone_datetime)

    # Relationships
    media_file: MediaFile = Relationship(back_populates="file_chunks")
