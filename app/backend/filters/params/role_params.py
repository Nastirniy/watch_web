"""
RoleFilterParams - Strongly typed filter parameters for Role
"""

from typing import Optional
from datetime import datetime
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class RoleFilterParams(BaseFilterParams):
    """Filter parameters for Role search"""

    id: Optional[int] = Field(default=None, description="Exact role ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    display_name: Optional[str] = Field(default=None, description="Exact display name")
    display_name_contains: Optional[str] = Field(default=None, max_length=50, description="Display name contains text")
    description_contains: Optional[str] = Field(default=None, max_length=100, description="Description contains text")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")
