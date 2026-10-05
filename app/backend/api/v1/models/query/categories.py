"""
CategoryFilterParams - Strongly typed filter parameters for Category
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
CategorySortField = Literal["id", "name", "alias"]


class CategoryFilterParams(BaseFilterParams):
    """Filter parameters for Category search"""

    sort_by: Optional[CategorySortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact category ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    alias: Optional[str] = Field(default=None, description="Exact alias")
    alias_contains: Optional[str] = Field(default=None, max_length=50, description="Alias contains text")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires category.archive)")
