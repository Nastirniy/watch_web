"""
TagFilterParams - Strongly typed filter parameters for Tag
"""

from typing import Optional
from datetime import datetime
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class TagFilterParams(BaseFilterParams):
    """Filter parameters for Tag search"""

    id: Optional[int] = Field(default=None, description="Exact tag ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include soft-deleted records")
