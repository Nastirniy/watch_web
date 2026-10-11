"""
MediaFilterParams - Strongly typed filter parameters for MediaFile
"""

from typing import Optional, Literal
from datetime import datetime
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
MediaSortField = Literal["id", "original_filename", "file_size", "media_type", "uploaded_at", "observation_id"]


class MediaFilterParams(BaseFilterParams):
    """Filter parameters for MediaFile search"""

    sort_by: Optional[MediaSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact media ID")
    observation_id: Optional[int] = Field(default=None, description="Filter by observation ID")
    original_filename_contains: Optional[str] = Field(default=None, max_length=100, description="Filename contains text")
    media_type: Optional[str] = Field(default=None, description="Media type (image, video, audio, document)")
    mime_type: Optional[str] = Field(default=None, description="Exact MIME type")
    file_extension: Optional[str] = Field(default=None, description="File extension (jpg, mp4, etc)")
    file_size_from: Optional[int] = Field(default=None, ge=0, description="File size min (bytes)")
    file_size_to: Optional[int] = Field(default=None, ge=0, description="File size max (bytes)")
    width_from: Optional[int] = Field(default=None, ge=0, description="Width min (pixels)")
    width_to: Optional[int] = Field(default=None, ge=0, description="Width max (pixels)")
    height_from: Optional[int] = Field(default=None, ge=0, description="Height min (pixels)")
    height_to: Optional[int] = Field(default=None, ge=0, description="Height max (pixels)")
    has_thumbnail: Optional[bool] = Field(default=None, description="Has a thumbnail available (images always, videos once the poster is ready)")
    uploaded_at_from: Optional[datetime] = Field(default=None, description="Uploaded after")
    uploaded_at_to: Optional[datetime] = Field(default=None, description="Uploaded before")
    include_deleted: bool = Field(default=False, description="Include archived media (requires media.archive); media of archived observations are never listed")
