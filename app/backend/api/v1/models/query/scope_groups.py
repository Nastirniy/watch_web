"""
ScopeGroupFilterParams - Strongly typed filter parameters for scope groups
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
ScopeGroupSortField = Literal["id", "name", "created_at", "updated_at"]


class ScopeGroupFilterParams(BaseFilterParams):
    """Filter parameters for scope group search"""

    sort_by: Optional[ScopeGroupSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact group ID")
    parent_id: Optional[int] = Field(default=None, description="Direct children of this group")
    name_contains: Optional[str] = Field(default=None, max_length=255, description="Name contains text")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires scope_group.archive)")
