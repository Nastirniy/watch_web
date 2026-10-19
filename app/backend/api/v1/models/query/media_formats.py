"""
MediaFormatFilterParams - Strongly typed filter parameters for MediaFormat
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.general import MediaType

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
MediaFormatSortField = Literal["id", "media_type", "content_type", "label", "max_file_size_bytes", "sort_order", "is_active"]


class MediaFormatFilterParams(BaseFilterParams):
    """Filter parameters for accepted media formats"""

    sort_by: Optional[MediaFormatSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact format ID")
    media_type: Optional[MediaType] = Field(default=None, description="Media type (formats exist for image, video and audio)")
    content_type: Optional[str] = Field(default=None, description="Exact Content-Type, e.g. image/jpeg")
    content_type_contains: Optional[str] = Field(default=None, max_length=50, description="Content-Type contains text")
    label_contains: Optional[str] = Field(default=None, max_length=50, description="Label contains text")
    is_active: Optional[bool] = Field(default=None, description="Only active (accepted) or only disabled formats")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires media_format.archive)")
