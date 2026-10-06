"""
PostFilterParams - Strongly typed filter parameters for Post
"""

from typing import Optional, Literal
from datetime import datetime
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
PostSortField = Literal["id", "name", "code", "city", "created_at", "updated_at", "last_user_activity_at", "is_active", "online_user_count", "user_count"]


class PostFilterParams(BaseFilterParams):
    """Filter parameters for Post search"""

    sort_by: Optional[PostSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact post ID")
    name: Optional[str] = Field(default=None, description="Exact name")
    name_contains: Optional[str] = Field(default=None, max_length=50, description="Name contains text")
    code: Optional[str] = Field(default=None, description="Exact code")
    code_contains: Optional[str] = Field(default=None, max_length=50, description="Code contains text")
    region: Optional[str] = Field(default=None, description="Exact region")
    region_contains: Optional[str] = Field(default=None, max_length=50, description="Region contains text")
    city: Optional[str] = Field(default=None, description="Exact city")
    city_contains: Optional[str] = Field(default=None, max_length=50, description="City contains text")
    description_contains: Optional[str] = Field(default=None, max_length=100, description="Description contains text")
    is_active: Optional[bool] = Field(default=None, description="Filter by active status")
    has_online_users: Optional[bool] = Field(default=None, description="true: posts with at least one home user online now; false: posts without")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")
    last_user_activity_from: Optional[datetime] = Field(default=None, description="Latest home user activity after")
    last_user_activity_to: Optional[datetime] = Field(default=None, description="Latest home user activity before")

