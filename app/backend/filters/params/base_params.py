"""
BaseFilterParams - Common pagination and sorting for all entities
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class SortOrder(str, Enum):
    """Sort order options (SQLAlchemy-compatible)"""
    ASCENDING = "ascending"
    DESCENDING = "descending"
    ASCENDING_NULLS_FIRST = "ascending_nulls_first"
    ASCENDING_NULLS_LAST = "ascending_nulls_last"
    DESCENDING_NULLS_FIRST = "descending_nulls_first"
    DESCENDING_NULLS_LAST = "descending_nulls_last"


class BaseFilterParams(BaseModel):
    """Common pagination and sorting parameters"""

    page: int = Field(default=1, ge=1, description="Page number")
    page_size: int = Field(default=50, ge=1, le=200, description="Items per page")
    sort_by: Optional[str] = Field(default=None, description="Field to sort by (see /filter-schema for valid values)")
    sort_order: SortOrder = Field(default=SortOrder.ASCENDING, description="Sort order")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
