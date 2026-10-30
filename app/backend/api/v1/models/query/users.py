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
    post_id: Optional[int] = Field(default=None, description="Users attached to this post: home post, or an assignment at it (see membership). Before, only the home post: pass membership=home for that (what PostResponse.user_count counts)")
    corps_id: Optional[int] = Field(default=None, description="Users attached to this corps: home node in it (its brigades included), or an assignment at it or below it (see membership); an id of another kind gives nothing. A group outside every subtree where the caller holds scope_group.view answers 403 permission_required_for_action, an unknown id too")
    brigade_id: Optional[int] = Field(default=None, description="Users attached to this brigade: home node in it, or an assignment at it or below it (see membership); an id of another kind gives nothing. A group outside every subtree where the caller holds scope_group.view answers 403 permission_required_for_action, an unknown id too")
    membership: Literal["any", "home", "assigned"] = Field(default="any", description="How post_id, brigade_id and corps_id match: home (the home node is there), assigned (a role assignment at the node or below it; archived nodes and roles do not count) or any (either). Base roles sit at the home post, so assigned usually includes the home users too. No effect without a unit field")
    user_type: Optional[UserType] = Field(default=None, description="Users of this type (GET /user-types)")
    is_active: Optional[bool] = Field(default=None, description="Filter by active status")
    is_online: Optional[bool] = Field(default=None, description="Online now (session touched within the online threshold)")
    created_at_from: Optional[UtcDatetime] = Field(default=None, description="Created after")
    created_at_to: Optional[UtcDatetime] = Field(default=None, description="Created before")
    last_login_from: Optional[UtcDatetime] = Field(default=None, description="Last login after")
    last_login_to: Optional[UtcDatetime] = Field(default=None, description="Last login before")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived users (requires user.archive held anywhere; archived users only where user.archive covers them)")
