from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


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
    thumbnail_id: Optional[int] = None


class UploadSessionInfo(BaseModel):
    media_file_id: int
    uploaded_chunks: List[int]
    total_chunks: int
    file_hash: str
    last_activity: datetime
    is_expired: bool
    can_resume: bool


class ThumbnailInfo(BaseModel):
    media_id: int
    thumbnail_url: Optional[str]
    thumbnail_ready: bool
    error: Optional[str]
