from typing import Optional, List
from pydantic import BaseModel, Field

from app.backend.enums.errors import RequiredPermissions
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.utils.validators import PlainTextModel


class RoleBriefResponse(PlainTextModel):
    id: int
    name: str
    display_name: str


class AuthorizationInfo(PlainTextModel):
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
    """A login session; times are ISO 8601 in UTC with the offset"""
    id: str
    status: SessionStatus = Field(description="Computed when read: online, idle (live, client quiet) or ended")
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str
    expires_at: str = Field(
        description="When the session ends unless a request comes before: every request and refresh moves it "
                    "(never past login + security.session_max_hours when that is set)")
    last_activity_at: Optional[str] = None


class UserSessionResponse(SessionInfoResponse):
    """A session as administrators see it, ended ones included"""
    ended_at: Optional[str] = None
    end_reason: Optional[SessionEndReason] = Field(
        default=None, description="Ended sessions: logout, expired, idle_timeout or revoked (null for sessions "
                                  "ended before reasons were recorded)")
    revoke_cause: Optional[SessionRevokeCause] = Field(
        default=None, description="Revoked sessions: what made the server end it; the account may be unchanged")


class UserSessionsResponse(BaseModel):
    sessions: list[SessionInfoResponse]


class SessionsEndedResponse(BaseModel):
    ended_count: int = Field(description="Live sessions that were revoked")


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
