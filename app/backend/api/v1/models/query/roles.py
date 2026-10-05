"""
RoleFilterParams - Strongly typed filter parameters for Role
"""

from typing import Optional, Literal
from datetime import datetime
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
RoleSortField = Literal["id", "name", "display_name", "created_at", "updated_at"]


class RoleFilterParams(BaseFilterParams):
    """Filter parameters for Role search"""

    sort_by: Optional[RoleSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact role ID")
    include_deleted: bool = Field(default=False, description="Include archived roles (requires role.archive)")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    display_name: Optional[str] = Field(default=None, description="Exact display name")
    display_name_contains: Optional[str] = Field(default=None, max_length=50, description="Display name contains text")
    description_contains: Optional[str] = Field(default=None, max_length=100, description="Description contains text")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")
