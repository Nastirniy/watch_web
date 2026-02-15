"""
Search request models for the unified search API
"""

from typing import List, Optional, Any
from pydantic import BaseModel, field_validator

from app.backend.filters.base import FilterOperator, FilterCondition, SortOrder, BaseFilterParams


class SearchRequest(BaseModel):
    """Request model for search endpoint"""
    conditions: List[FilterCondition] = []
    logic: str = "and"
    sort: List[SortOrder] = []
    page: int = 1
    page_size: int = 50
    include_deleted: bool = False

    model_config = {"extra": "forbid"}

    @field_validator('page')
    @classmethod
    def validate_page(cls, v: int) -> int:
        return max(1, v)

    @field_validator('page_size')
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        return min(max(1, v), 200)

    @field_validator('logic')
    @classmethod
    def validate_logic(cls, v: str) -> str:
        if v not in ("and", "or"):
            raise ValueError("logic must be 'and' or 'or'")
        return v

    def to_filter_params(self) -> BaseFilterParams:
        """Convert to BaseFilterParams for filter engine"""
        return BaseFilterParams(
            conditions=self.conditions,
            logic=self.logic,
            sort=self.sort,
            page=self.page,
            page_size=self.page_size,
            include_deleted=self.include_deleted
        )
