"""
BaseFilterParams - Common pagination and sorting for all entities
"""

from typing import Optional
from pydantic import BaseModel, Field

from app.backend.enums.general import SortOrder


class BaseFilterParams(BaseModel):
    """Common pagination and sorting parameters"""

    page: int = Field(default=1, ge=1, description="Page number")
    page_size: int = Field(default=50, ge=1, le=200, description="Items per page")
    sort_by: Optional[str] = Field(default=None, description="Field to sort by; each entity narrows it to its sortable fields")
    sort_order: SortOrder = Field(default=SortOrder.ASCENDING, description="Sort order")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
