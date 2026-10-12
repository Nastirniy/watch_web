"""
UserFilterParams - Strongly typed filter parameters for User
"""

from typing import Optional, Literal
from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.general import UserType
from app.backend.utils.validators import UtcDatetime

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
UserSortField = Literal["id", "username", "username_alias", "created_at", "updated_at", "last_login_at", "is_active", "last_activity_at"]


class UserFilterParams(BaseFilterParams):
    """Filter parameters for User search"""

    sort_by: Optional[UserSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact user ID")
    username: Optional[str] = Field(default=None, description="Exact username")
    username_contains: Optional[str] = Field(default=None, max_length=50, description="Username contains text")
    username_alias: Optional[str] = Field(default=None, description="Exact username alias")
    username_alias_contains: Optional[str] = Field(default=None, max_length=50, description="Alias contains text")
    post_id: Optional[int] = Field(default=None, description="Filter by post ID")
    user_type: Optional[UserType] = Field(default=None, description="Users of this type (GET /user-types)")
    is_active: Optional[bool] = Field(default=None, description="Filter by active status")
    is_online: Optional[bool] = Field(default=None, description="Online now (session touched within the online threshold)")
    created_at_from: Optional[UtcDatetime] = Field(default=None, description="Created after")
    created_at_to: Optional[UtcDatetime] = Field(default=None, description="Created before")
    last_login_from: Optional[UtcDatetime] = Field(default=None, description="Last login after")
    last_login_to: Optional[UtcDatetime] = Field(default=None, description="Last login before")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires user.archive)")
