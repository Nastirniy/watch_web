from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime


class MediaFileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_filename: str
    stored_filename: str
    file_size: int
    mime_type: str
    media_type: str
    width: Optional[int]
    height: Optional[int]
    uploaded_at: datetime
    thumbnail_id: Optional[int] = None


class ThumbnailInfo(BaseModel):
    media_id: int
    thumbnail_url: Optional[str]
    thumbnail_ready: bool
    error: Optional[str]
