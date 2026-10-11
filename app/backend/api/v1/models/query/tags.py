"""
TagFilterParams - Strongly typed filter parameters for Tag
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.utils.validators import UtcDatetime

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
TagSortField = Literal["id", "name", "created_at"]


class TagFilterParams(BaseFilterParams):
    """Filter parameters for Tag search"""

    sort_by: Optional[TagSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact tag ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    created_at_from: Optional[UtcDatetime] = Field(default=None, description="Created after")
    created_at_to: Optional[UtcDatetime] = Field(default=None, description="Created before")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires tag.archive)")
