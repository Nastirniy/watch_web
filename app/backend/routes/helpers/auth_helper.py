from app.backend.config import settings
from app.backend.services.auth_context import AuthContext
from app.backend.services.presence import online_threshold_seconds
from app.backend.routes.models.response.auth_response import (
    LoginResponse,
    UserInfoResponse,
    RoleBriefResponse,
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


def build_login_response(auth: AuthContext, access_token: str, refresh_token: str) -> LoginResponse:
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.security.access_token_expire_minutes * 60,
        user_id=auth.user.id,
        username=auth.user.username,
        **_authorization_fields(auth),
    )
