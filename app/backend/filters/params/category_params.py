"""
CategoryFilterParams - Strongly typed filter parameters for Category
"""

from typing import Optional
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class CategoryFilterParams(BaseFilterParams):
    """Filter parameters for Category search"""

    id: Optional[int] = Field(default=None, description="Exact category ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    alias: Optional[str] = Field(default=None, description="Exact alias")
    alias_contains: Optional[str] = Field(default=None, max_length=50, description="Alias contains text")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires category.archive)")
