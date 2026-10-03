from datetime import datetime
from typing import Optional

from app.backend.models.user import UserSession
from app.backend.services.auth_context import AuthContext
from app.backend.services.presence import online_threshold_seconds
from app.backend.services.sessions import SessionState, utc
from app.backend.api.shared.models.response.auth_response import (
    LoginResponse,
    UserInfoResponse,
    RoleBriefResponse,
    UserSessionResponse,
)


def _authorization_fields(auth: AuthContext) -> dict:
    return {
        "post_id": auth.user.post_id,
        "is_admin": auth.is_admin,
        "permissions": sorted(auth.permissions),
        "roles": [RoleBriefResponse(id=r.id, name=r.name, display_name=r.display_name) for r in auth.roles],
        "accessible_post_ids": auth.accessible_post_ids_api(),
    }


def build_user_info(auth: AuthContext) -> UserInfoResponse:
    user = auth.user
    return UserInfoResponse(
        id=user.id,
        username=user.username,
        username_alias=user.username_alias,
        is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        last_activity_at=user.last_activity_at.isoformat() if getattr(user, "last_activity_at", None) else None,
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


def _iso(value: Optional[datetime]) -> Optional[str]:
    """ISO 8601 in UTC with the offset: the database returns naive UTC, which JavaScript would read as local time"""
    return utc(value).isoformat() if value is not None else None


def build_session_response(row: UserSession, state: SessionState) -> UserSessionResponse:
    """A session with its computed state; the end fields are present only for ended sessions"""
    return UserSessionResponse(
        id=row.id,
        status=state.status,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        created_at=_iso(row.created_at),
        expires_at=_iso(row.expires_at),
        idle_expires_at=_iso(state.idle_expires_at),
        last_activity_at=_iso(row.last_activity),
        ended_at=_iso(state.ended_at),
        end_reason=state.end_reason,
        revoke_cause=state.revoke_cause,
    )
