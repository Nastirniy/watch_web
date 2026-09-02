from typing import Optional, List
from pydantic import BaseModel, Field

from app.backend.routes.models.response.error_response import RequiredPermissions


class RoleBriefResponse(BaseModel):
    id: int
    name: str
    display_name: str


class AuthorizationInfo(BaseModel):
    """Authorization data the GUI needs to build its interface"""
    post_id: Optional[int] = Field(default=None, description="Home post of the user")
    is_admin: bool = Field(description="Holds admin.true: may open the administration area")
    permissions: List[str] = Field(default_factory=list, description="Effective permission names")
    roles: List[RoleBriefResponse] = Field(default_factory=list)
    accessible_post_ids: Optional[List[int]] = Field(
        default=None,
        description="Posts whose data the user may read. null means every post (post.access_all)"
    )


class LoginResponse(AuthorizationInfo):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class LogoutResponse(BaseModel):
    message: str


class UserInfoResponse(AuthorizationInfo):
    id: int
    username: str
    username_alias: Optional[str] = None
    is_active: bool
    last_login_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    online_threshold_seconds: int = Field(description="Seconds of inactivity after which a user counts as offline")


class SessionInfoResponse(BaseModel):
    id: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str
    expires_at: str
    last_activity_at: Optional[str] = None


class UserSessionsResponse(BaseModel):
    sessions: list[SessionInfoResponse]


class PermissionCheckResponse(BaseModel):
    allowed: bool
    missing: List[str] = Field(default_factory=list, description="Granular permissions the caller lacks")
    admin_required: bool = Field(description="True when admin.true was required and the caller lacks it")


class RouteAccessEntryResponse(BaseModel):
    method: str
    path: str
    name: str
    tags: List[str] = Field(default_factory=list)
    required: RequiredPermissions


class RouteAccessResponse(BaseModel):
    routes: List[RouteAccessEntryResponse]
