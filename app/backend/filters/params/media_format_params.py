"""
MediaFormatFilterParams - Strongly typed filter parameters for MediaFormat
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
MediaFormatSortField = Literal["id", "media_type", "content_type", "label", "max_file_size_bytes", "sort_order", "is_active"]


class MediaFormatFilterParams(BaseFilterParams):
    """Filter parameters for accepted media formats"""

    sort_by: Optional[MediaFormatSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact format ID")
    media_type: Optional[str] = Field(default=None, description="image, video or audio")
    content_type: Optional[str] = Field(default=None, description="Exact Content-Type, e.g. image/jpeg")
    content_type_contains: Optional[str] = Field(default=None, max_length=50, description="Content-Type contains text")
    label_contains: Optional[str] = Field(default=None, max_length=50, description="Label contains text")
    is_active: Optional[bool] = Field(default=None, description="Only active (accepted) or only disabled formats")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires media_format.archive)")
