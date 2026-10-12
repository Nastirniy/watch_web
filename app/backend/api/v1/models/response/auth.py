from typing import Optional, List
from pydantic import BaseModel, Field

from app.backend.enums.errors import RequiredPermissions
from app.backend.enums.general import Permissions, SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.models.user import UserSession
from app.backend.auth.context import AuthContext
from app.backend.auth.presence import online_threshold_seconds
from app.backend.auth.sessions import SessionState
from app.backend.utils.validators import PlainTextModel, ResponseDatetime


class RoleBriefResponse(PlainTextModel):
    id: int
    name: str
    display_name: str


class AuthorizationInfo(PlainTextModel):
    """Authorization data the GUI needs to build its interface"""
    post_id: Optional[int] = Field(default=None, description="Home post of the user")
    is_admin: bool = Field(description="Holds the built-in admin role at the root (no route checks it, every route "
                                       "checks its own permissions)")
    permissions: List[str] = Field(default_factory=list, description="Permissions held at some node of the scope tree")
    roles: List[RoleBriefResponse] = Field(default_factory=list, description="The roles of the user's assignments")
    accessible_post_ids: Optional[List[int]] = Field(
        default=None,
        description="Posts where observation.view is in effect (archived posts only through a group or the root). "
                    "null means every post (observation.view at the root)"
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
    last_login_at: Optional[ResponseDatetime] = None
    last_activity_at: Optional[ResponseDatetime] = None
    online_threshold_seconds: int = Field(description="Seconds of inactivity after which a user counts as offline")


class SessionInfoResponse(BaseModel):
    """A login session; times are ISO 8601 in UTC with the offset"""
    id: str
    status: SessionStatus = Field(description="Computed when read: online, idle (live, client quiet) or ended")
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: ResponseDatetime
    expires_at: ResponseDatetime = Field(
        description="When the session ends unless a request comes before: every request and refresh moves it "
                    "(never past login + security.session_max_hours when that is set)")
    last_activity_at: Optional[ResponseDatetime] = None


class UserSessionResponse(SessionInfoResponse):
    """A session as the session routes of a user list it, ended ones included"""
    ended_at: Optional[ResponseDatetime] = None
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
    missing: List[str] = Field(default_factory=list, description="Permissions the caller lacks")


class RouteAccessEntryResponse(BaseModel):
    method: str
    path: str
    name: str
    tags: List[str] = Field(default_factory=list)
    required: RequiredPermissions


class RouteAccessResponse(BaseModel):
    routes: List[RouteAccessEntryResponse]


def _authorization_fields(auth: AuthContext) -> dict:
    readable = auth.posts_with(Permissions.OBSERVATION_VIEW)
    return {
        "post_id": auth.user.post_id,
        "is_admin": auth.is_admin,
        "permissions": sorted(auth.permissions),
        "roles": [RoleBriefResponse(id=r.id, name=r.name, display_name=r.display_name) for r in auth.roles],
        "accessible_post_ids": None if readable is None else sorted(readable),
    }


def build_user_info(auth: AuthContext) -> UserInfoResponse:
    user = auth.user
    return UserInfoResponse(
        id=user.id,
        username=user.username,
        username_alias=user.username_alias,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        last_activity_at=getattr(user, "last_activity_at", None),
        online_threshold_seconds=online_threshold_seconds(),
        **_authorization_fields(auth),
    )


def build_login_response(auth: AuthContext, access_token: str, refresh_token: str, expires_in: int) -> LoginResponse:
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user_id=auth.user.id,
        username=auth.user.username,
        **_authorization_fields(auth),
    )


def build_session_response(row: UserSession, state: SessionState) -> UserSessionResponse:
    """A session with its computed state; the end fields are set only for ended sessions"""
    ended = state.status is SessionStatus.ENDED
    return UserSessionResponse(
        id=row.id,
        status=state.status,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        created_at=row.created_at,
        expires_at=row.expires_at if ended else state.ends_at,
        last_activity_at=row.last_activity,
        ended_at=state.ends_at if ended else None,
        end_reason=state.end_reason,
        revoke_cause=state.revoke_cause,
    )
