from typing import Dict, Iterable, Optional, List
from pydantic import BaseModel, Field

from app.backend.enums.errors import RequiredPermissions
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus, UserType
from app.backend.models.user import UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import certificate_state
from app.backend.api.v1.models.response.assignments import AssignmentResponse
from app.backend.api.v1.models.response.certificates import SessionCertificate
from app.backend.auth.assignments import AssignmentView
from app.backend.auth.context import AuthContext
from app.backend.auth.presence import online_threshold_seconds
from app.backend.auth.sessions import SessionState
from app.backend.utils.validators import PlainTextModel, ResponseDatetime


class AuthorizationInfo(PlainTextModel):
    """Authorization data the web client builds its interface from: the pages by user_type, the menu by permissions,
    the buttons of a post by root_permissions plus the entry of the post in post_permissions. No route authorizes by
    the type: every route checks its own permissions."""
    post_id: Optional[int] = Field(description="Home post of the user (membership: who manages them); null for none")
    user_type: UserType = Field(description="The type of the user (GET /user-types): which pages the web client shows")
    permissions: List[str] = Field(description="Permissions held at some node of the scope tree (the menu)")
    assignments: List[AssignmentResponse] = Field(
        description="The role assignments of the user, as GET /users/{user_id}/assignments lists them (assignments of "
                    "archived roles included: role.is_deleted, they grant nothing)")
    root_permissions: List[str] = Field(description="Permissions held at the root: in effect at every post")
    post_permissions: Dict[str, List[str]] = Field(
        description="Post id -> the permissions the assignments at groups and posts put in effect at that post, for "
                    "every post they reach (an assignment on an archived post stops applying there, a group's keep "
                    "applying to its archived posts). In effect at a post = root_permissions plus its entry")


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
    certificate: Optional[SessionCertificate] = Field(
        default=None, description="The client certificate the current session was opened with (warn before it "
                                  "expires); null for a session bound to none (development login)")


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


def _authorization_fields(auth: AuthContext, assignments: Iterable[AssignmentView]) -> dict:
    return {
        "post_id": auth.user.post_id,
        "user_type": auth.user.user_type,
        "permissions": sorted(auth.permissions),
        "assignments": [AssignmentResponse.model_validate(view, from_attributes=True) for view in assignments],
        "root_permissions": sorted(auth.root_permissions),
        "post_permissions": {str(post_id): sorted(held) for post_id, held in auth.post_permissions().items()},
    }


def build_user_info(auth: AuthContext, assignments: Iterable[AssignmentView],
                    certificate: Optional[UserCertificate] = None) -> UserInfoResponse:
    """assignments: auth/assignments.py assignment_views of the user; certificate: the one the session is bound to
    (services/certificates.py session_certificate)"""
    user = auth.user
    return UserInfoResponse(
        id=user.id,
        username=user.username,
        username_alias=user.username_alias,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        last_activity_at=getattr(user, "last_activity_at", None),
        online_threshold_seconds=online_threshold_seconds(),
        certificate=None if certificate is None else SessionCertificate(
            id=certificate.id, label=certificate.label, not_after=certificate.not_after,
            state=certificate_state(certificate)),
        **_authorization_fields(auth, assignments),
    )


def build_login_response(auth: AuthContext, assignments: Iterable[AssignmentView], access_token: str,
                         refresh_token: str, expires_in: int) -> LoginResponse:
    """assignments: auth/assignments.py assignment_views of the user"""
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user_id=auth.user.id,
        username=auth.user.username,
        **_authorization_fields(auth, assignments),
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
