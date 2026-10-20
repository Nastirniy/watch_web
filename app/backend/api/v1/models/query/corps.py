"""
UnitFilterParams - Strongly typed filter parameters for corps (GET /corps) and the brigades of a corps
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
UnitSortField = Literal["id", "name", "created_at", "updated_at"]


class UnitFilterParams(BaseFilterParams):
    """Filter parameters for a list of corps or brigades"""

    sort_by: Optional[UnitSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact ID")
    name_contains: Optional[str] = Field(default=None, max_length=255, description="Name contains text")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires scope_group.archive)")
