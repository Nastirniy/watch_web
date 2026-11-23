import ipaddress
from typing import Dict, Iterable, Optional, List
from pydantic import Field

from app.backend.enums.errors import RequiredPermissions
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus, UserType
from app.backend.models.user import UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import certificate_state
from app.backend.services.user_units import UserUnits
from app.backend.api.v1.models.response.assignments import AssignmentResponse, SystemAssignmentResponse
from app.backend.api.v1.models.response.certificates import SessionCertificate
from app.backend.api.v1.models.response.posts import UnitRefResponse
from app.backend.api.v1.models.response.users import HomeRefResponse, PostRefResponse, units_fields
from app.backend.auth.assignments import AssignmentView
from app.backend.auth.context import AuthContext
from app.backend.auth.presence import online_threshold_seconds
from app.backend.auth.sessions import SessionState
from app.backend.utils.validators import PlainTextModel, ResponseDatetime, ResponseModel


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
    system_assignments: List[SystemAssignmentResponse] = Field(
        description="The system role assignments of the user, as GET /users/{user_id}/system-assignments lists them")
    system_permissions: List[str] = Field(
        description="Permissions of the system roles: they act on the whole system (administration of roles, "
                    "dictionaries, formats, audit, monitoring, backups)")
    root_permissions: List[str] = Field(description="Tree permissions held at the root: in effect at every post")
    post_permissions: Dict[str, List[str]] = Field(
        description="Post id -> the permissions the assignments at groups and posts put in effect at that post, for "
                    "every post they reach (an assignment on an archived post stops applying there, a group's keep "
                    "applying to its archived posts). In effect at a post = root_permissions plus its entry")
    group_permissions: Dict[str, List[str]] = Field(
        description="Group id -> the permissions the assignments at groups put in effect at that group, for every "
                    "group they reach. In effect at a group = root_permissions plus its entry. Unit management acts "
                    "at the parent: a corps or a group directly under the root is managed with "
                    "root_permissions only, a brigade or a group inside another group with "
                    "root_permissions plus the entry of its parent")


class LoginResponse(AuthorizationInfo):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class LogoutResponse(ResponseModel):
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
    home: Optional[HomeRefResponse] = Field(default=None, description="The node the user belongs to now; null without a home")
    post: Optional[PostRefResponse] = Field(default=None, description="The home post of the user (post_id with its name and code); null without one")
    brigade: Optional[UnitRefResponse] = Field(default=None, description="Brigade the home sits in now; null: outside any brigade")
    corps: Optional[UnitRefResponse] = Field(default=None, description="Corps the home sits in now (directly or through a brigade); null: outside any corps")


class SessionInfoResponse(ResponseModel):
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


# Prefix lengths of the network a masked address shows (owner decision 2026-10-29)
MASK_PREFIX_IPV4 = 16
MASK_PREFIX_IPV6 = 48


class UserSessionResponse(SessionInfoResponse):
    """A session as the session routes of a user list it, ended ones included. Only the user themself sees the device
    of their sessions in clear; for anyone else ip_address is masked to its network and user_agent is null
    (device_masked true): the session's id, status and times stay, enough to end it."""
    ip_address: Optional[str] = Field(
        default=None, description="The client address of the login. Masked (device_masked) for a caller other than "
                                  "the user: the network in CIDR form, IPv4 /16 (192.168.0.0/16), IPv6 /48 "
                                  "(2001:db8:1::/48), null when the stored value is no address")
    user_agent: Optional[str] = Field(
        default=None, description="The User-Agent of the login; null when masked (device_masked)")
    device_masked: bool = Field(
        description="True when the caller is not the user of the session: ip_address shows only its network and "
                    "user_agent is null")
    ended_at: Optional[ResponseDatetime] = None
    end_reason: Optional[SessionEndReason] = Field(
        default=None, description="Ended sessions: logout, expired, idle_timeout or revoked (null for sessions "
                                  "ended before reasons were recorded)")
    revoke_cause: Optional[SessionRevokeCause] = Field(
        default=None, description="Revoked sessions: what made the server end it; the account may be unchanged")


class UserSessionsResponse(ResponseModel):
    sessions: list[SessionInfoResponse]


class SessionsEndedResponse(ResponseModel):
    ended_count: int = Field(description="Live sessions that were revoked")


class PermissionCheckResponse(ResponseModel):
    allowed: bool
    missing: List[str] = Field(default_factory=list, description="Permissions the caller lacks")


class RouteAccessEntryResponse(ResponseModel):
    method: str
    path: str
    name: str
    tags: List[str] = Field(default_factory=list)
    required: RequiredPermissions


class RouteAccessResponse(ResponseModel):
    routes: List[RouteAccessEntryResponse]


def _authorization_fields(auth: AuthContext, assignments: Iterable[AssignmentView],
                          system_assignments: Iterable) -> dict:
    return {
        "post_id": auth.user.post_id,
        "user_type": auth.user.user_type,
        "permissions": sorted(auth.permissions),
        "assignments": [AssignmentResponse.model_validate(view, from_attributes=True) for view in assignments],
        "system_assignments": [SystemAssignmentResponse.model_validate(view, from_attributes=True)
                               for view in system_assignments],
        "system_permissions": sorted(auth.system_permissions),
        "root_permissions": sorted(auth.root_permissions),
        "post_permissions": {str(post_id): sorted(held) for post_id, held in auth.post_permissions().items()},
        "group_permissions": {str(group_id): sorted(held) for group_id, held in auth.group_permissions().items()},
    }


def build_user_info(auth: AuthContext, assignments: Iterable[AssignmentView], system_assignments: Iterable,
                    certificate: Optional[UserCertificate] = None,
                    units: Optional[UserUnits] = None) -> UserInfoResponse:
    """assignments: auth/assignments.py assignment_views of the user; system_assignments:
    auth/system_assignments.py system_assignment_views; certificate: the one the session is bound to
    (services/certificates.py session_certificate); units: services/user_units.py users_units of the user"""
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
        **units_fields(units or UserUnits()),
        **_authorization_fields(auth, assignments, system_assignments),
    )


def build_login_response(auth: AuthContext, assignments: Iterable[AssignmentView], system_assignments: Iterable,
                         access_token: str, refresh_token: str, expires_in: int) -> LoginResponse:
    """assignments: auth/assignments.py assignment_views of the user; system_assignments: system_assignment_views"""
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user_id=auth.user.id,
        username=auth.user.username,
        **_authorization_fields(auth, assignments, system_assignments),
    )


def masked_ip_address(value: Optional[str]) -> Optional[str]:
    """The network of an address in CIDR form: IPv4 /16, IPv6 /48 (an IPv4-mapped IPv6 address as its IPv4 network);
    None for a value that is no address"""
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        return str(ipaddress.IPv4Network((int(address), MASK_PREFIX_IPV4), strict=False))
    return str(ipaddress.IPv6Network((int(address), MASK_PREFIX_IPV6), strict=False))


def build_session_response(row: UserSession, state: SessionState, masked: bool = False) -> UserSessionResponse:
    """A session with its computed state; the end fields are set only for ended sessions. masked for every caller
    but the user of the session: the address cut to its network (masked_ip_address), no user agent."""
    ended = state.status is SessionStatus.ENDED
    return UserSessionResponse(
        id=row.id,
        status=state.status,
        ip_address=masked_ip_address(row.ip_address) if masked else row.ip_address,
        user_agent=None if masked else row.user_agent,
        device_masked=masked,
        created_at=row.created_at,
        expires_at=row.expires_at if ended else state.ends_at,
        last_activity_at=row.last_activity,
        ended_at=state.ends_at if ended else None,
        end_reason=state.end_reason,
        revoke_cause=state.revoke_cause,
    )
