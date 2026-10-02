"""
MediaFormatFilterParams - Strongly typed filter parameters for MediaFormat
"""

from typing import Optional
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class MediaFormatFilterParams(BaseFilterParams):
    """Filter parameters for accepted media formats"""

    id: Optional[int] = Field(default=None, description="Exact format ID")
    media_type: Optional[str] = Field(default=None, description="image, video or audio")
    content_type: Optional[str] = Field(default=None, description="Exact Content-Type, e.g. image/jpeg")
    content_type_contains: Optional[str] = Field(default=None, max_length=50, description="Content-Type contains text")
    label_contains: Optional[str] = Field(default=None, max_length=50, description="Label contains text")
    is_active: Optional[bool] = Field(default=None, description="Only active (accepted) or only disabled formats")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires media_format.archive)")
