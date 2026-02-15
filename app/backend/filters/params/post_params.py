"""
PostFilterParams - Strongly typed filter parameters for Post
"""

from typing import Optional
from datetime import datetime
from pydantic import Field

from app.backend.filters.params.base_params import BaseFilterParams


class PostFilterParams(BaseFilterParams):
    """Filter parameters for Post search"""

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
    connection_status: Optional[str] = Field(default=None, description="Connection status (online, offline, maintenance, error)")
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")
    last_connection_from: Optional[datetime] = Field(default=None, description="Last connection after")
    last_connection_to: Optional[datetime] = Field(default=None, description="Last connection before")
