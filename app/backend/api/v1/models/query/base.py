"""
BaseFilterParams - Common pagination and sorting for all entities
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from app.backend.enums.general import SortOrder


MAX_PAGE = 1_000_000  # page * page_size (200) stays far inside a 64-bit OFFSET


class BaseFilterParams(BaseModel):
    """Common pagination and sorting parameters"""

    # A misspelt filter answers 422 instead of widening the result unnoticed
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=MAX_PAGE, description="Page number")
    page_size: int = Field(default=50, ge=1, le=200, description="Items per page")
    sort_by: Optional[str] = Field(default=None, description="Field to sort by; each entity narrows it to its sortable fields")
    sort_order: SortOrder = Field(default=SortOrder.ASCENDING, description="Sort order")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
