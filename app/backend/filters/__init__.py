"""
Filters package - Simple typed filtering for API endpoints
"""

# Simple filter engine
from app.backend.filters.simple_engine import SimpleFilterEngine

# Filter services
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.filters.user_filter import UserFilterService
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.role_filter import RoleFilterService
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.filters.signal_type_filter import SignalTypeFilterService
from app.backend.filters.tag_filter import TagFilterService
from app.backend.filters.media_filter import MediaFilterService

# Filter params
from app.backend.filters.params import (
    BaseFilterParams,
    ObservationFilterParams,
    UserFilterParams,
    PostFilterParams,
    RoleFilterParams,
    CategoryFilterParams,
    SignalTypeFilterParams,
    TagFilterParams,
    MediaFilterParams,
)

__all__ = [
    # Engine
    "SimpleFilterEngine",
    # Services
    "ObservationFilterService",
    "UserFilterService",
    "PostFilterService",
    "RoleFilterService",
    "CategoryFilterService",
    "SignalTypeFilterService",
    "TagFilterService",
    "MediaFilterService",
    # Params
    "BaseFilterParams",
    "ObservationFilterParams",
    "UserFilterParams",
    "PostFilterParams",
    "RoleFilterParams",
    "CategoryFilterParams",
    "SignalTypeFilterParams",
    "TagFilterParams",
    "MediaFilterParams",
]