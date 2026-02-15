"""
SignalTypeFilterParams - Strongly typed filter parameters for SignalType
"""

from typing import Optional
from datetime import datetime
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class SignalTypeFilterParams(BaseFilterParams):
    """Filter parameters for SignalType search"""

    id: Optional[int] = Field(default=None, description="Exact signal type ID")
    code: Optional[str] = Field(default=None, description="Exact code")
    code_contains: Optional[str] = Field(default=None, max_length=50, description="Code contains text")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    description_contains: Optional[str] = Field(default=None, max_length=100, description="Description contains text")
    category_id: Optional[int] = Field(default=None, description="Filter by category ID")
    is_active: Optional[bool] = Field(default=None, description="Filter by active status")
    sort_order_from: Optional[int] = Field(default=None, description="Sort order min")
    sort_order_to: Optional[int] = Field(default=None, description="Sort order max")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include soft-deleted records")
