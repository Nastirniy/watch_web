from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.auth import AuthService
from app.backend.services.presence import is_expired
from datetime import datetime, timezone
from app.backend.services.permissions import require
from app.backend.services.auth_context import AuthContext, load_auth_context
from app.backend.services.permissions import Require
from app.backend.services.route_access import route_access_map, routes_allowed_for
from app.backend.api.shared.helpers.auth_helper import build_user_info, build_login_response
from app.backend.enums.errors import AuthErrorCode, ErrorCode, RequiredPermissions
from app.backend.models.user import User, UserSession
from app.backend.enums.routes import AuthRoutes
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.enums.general import AppEnvironment
from app.backend.enums.errors import AuthErrorCode
from app.backend.utils.logging_config import get_auth_logger
from app.backend.config import settings
from app.backend.helpers.certeficates_helper import parse_cn
from app.backend.api.shared.models.request.auth_request import (
    LoginRequest,
    RefreshRequest,
    LogoutRequest,
    PermissionCheckRequest
)
from app.backend.api.shared.models.response.auth_response import (
    LoginResponse,
    LogoutResponse,
    UserInfoResponse,
    SessionInfoResponse,
    UserSessionsResponse,
    PermissionCheckResponse,
    RouteAccessResponse,
    RouteAccessEntryResponse
)
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import ErrorCode
from app.backend.openapi_problems import problems

router = APIRouter(prefix=AuthRoutes.BASE.value, tags=[AuthRoutes.tag()])
_logger = get_auth_logger()


@router.post(
    AuthRoutes.LOGIN.value,
    response_model=LoginResponse,
    responses=problems(
        ErrorCode.LOGIN_DATA_REQUIRED, AuthErrorCode.ACCOUNT_DEACTIVATED, AuthErrorCode.ACCOUNT_DELETED,
        AuthErrorCode.CERTIFICATE_DATA_MISSING, AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED,
        AuthErrorCode.CERTIFICATE_VERIFICATION_FAILED, ErrorCode.AUTHENTICATION_UNAVAILABLE,
    ),
)
async def login(
        request: Request,
        login_data: Optional[LoginRequest] = None,
        session: AsyncSession = Depends(get_session),
        # Set by nginx from the verified client certificate (production), never by the client
        x_ssl_client_verify: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_dn: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_fingerprint: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_serial: Optional[str] = Header(None, include_in_schema=False),
):
    """Authenticate user with PKI certificate using SSL headers or JSON data for development"""
    try:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        # Check if we're in development mode and should use JSON data
        if settings.app.environment == AppEnvironment.DEVELOPMENT.value:
            if login_data:
                # Development mode: use JSON data from request body
                cn = login_data.certificate_cn
                ssl_serial = login_data.certificate_serial
                ssl_fingerprint = login_data.certificate_fingerprint

                _logger.info(f"Development mode: Using JSON authentication data from IP: {client_ip}")
            else:
                _logger.warning(f"Development mode: No authentication data provided from IP: {client_ip}")
                raise ApiError(ErrorCode.LOGIN_DATA_REQUIRED)

        else:
            # Production mode: extract SSL certificate data from headers
            ssl_verify = x_ssl_client_verify
            ssl_dn = x_ssl_client_dn
            ssl_fingerprint = x_ssl_client_fingerprint
            ssl_serial = x_ssl_client_serial

            # Verify that certificate was validated by nginx
            if ssl_verify != "SUCCESS":
                _logger.warning(
                    f"Certificate verification failed from IP: {client_ip}, "
                    f"status: {ssl_verify}"
                )
                raise ApiError(AuthErrorCode.CERTIFICATE_VERIFICATION_FAILED)

            # Parse CN from Distinguished Name
            cn = await parse_cn(ssl_dn)

            # Validate required certificate data
            if not cn or not ssl_serial or not ssl_fingerprint:
                _logger.warning(
                    f"Missing certificate data from IP: {client_ip}, "
                    f"CN: {cn}, Serial: {ssl_serial}, Fingerprint: {ssl_fingerprint}"
                )
                raise ApiError(AuthErrorCode.CERTIFICATE_DATA_MISSING)

        # Authenticate with enhanced PKI method
        user, auth_error = await AuthService.authenticate_pki(
            session,
            certificate_cn=cn,
            certificate_serial=ssl_serial,
            certificate_fingerprint=ssl_fingerprint,
            ip_address=client_ip
        )
        if not user:
            _logger.warning(
                f"Failed login attempt from IP: {client_ip}, "
                f"CN: {cn}, Serial: {ssl_serial}, error: {auth_error}"
            )

            raise ApiError(AuthErrorCode(auth_error or AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED))

        # Create session
        user_session = await AuthService.create_session(
            session,
            user,
            ip_address=client_ip,
            user_agent=user_agent,
            request_url=str(request.url)
        )

        # Create both access and refresh tokens with session binding
        access_token = AuthService.create_access_token(user.id, session_id=user_session.id)
        refresh_token = AuthService.create_refresh_token(user.id, session_id=user_session.id)

        _logger.info(
            f"Successful login for user {user.username} from IP: {client_ip}, "
            f"CN: {cn}, Serial: {ssl_serial}"
        )

        auth_context = await load_auth_context(session, user)
        return build_login_response(auth_context, access_token, refresh_token)

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Login error: {str(e)}")
        raise ApiError(ErrorCode.AUTHENTICATION_UNAVAILABLE)



@router.post(
    AuthRoutes.REFRESH.value,
    response_model=LoginResponse,
    responses=problems(
        AuthErrorCode.ACCOUNT_DEACTIVATED, AuthErrorCode.ACCOUNT_DELETED, AuthErrorCode.ACCOUNT_NOT_FOUND,
        AuthErrorCode.INVALID_REFRESH_TOKEN, AuthErrorCode.SESSION_EXPIRED, ErrorCode.TOKEN_REFRESH_FAILED,
    ),
)
async def refresh_token(
        request: Request,
        refresh_request: RefreshRequest,
        session: AsyncSession = Depends(get_session)
):
    """Refresh access token using refresh token"""
    try:
        # Verify refresh token
        payload = AuthService.verify_token(refresh_request.refresh_token, "refresh")
        if not payload:
            raise ApiError(AuthErrorCode.INVALID_REFRESH_TOKEN)

        user_id = int(payload.get("sub"))
        session_id = payload.get("sid", None)

        # Verify user still exists
        from sqlmodel import select
        user_result = await session.exec(
            select(User).where(User.id == user_id)
        )
        user = user_result.first()

        if not user:
            raise ApiError(AuthErrorCode.ACCOUNT_NOT_FOUND)

        if user.is_deleted or not user.is_active:
            # The refresh token of a deactivated or archived account is dead; end any session still live
            ended = await AuthService.expire_sessions_of_inactive_user(session, user)
            _logger.warning(f"Refresh attempt for inactive account: user_id={user_id}, sessions ended={ended}")
            if user.is_deleted:
                raise ApiError(AuthErrorCode.ACCOUNT_DELETED)
            raise ApiError(AuthErrorCode.ACCOUNT_DEACTIVATED)

        # Verify the session is still active (reject if sessions were invalidated)
        if session_id:
            session_result = await session.exec(
                select(UserSession).where(
                    UserSession.id == session_id,
                    UserSession.user_id == user_id,
                    UserSession.is_active == True
                )
            )
            refresh_session = session_result.first()
            if refresh_session and is_expired(refresh_session.expires_at):
                refresh_session.is_active = False
                refresh_session.ended_at = datetime.now(timezone.utc)
                session.add(refresh_session)
                await session.commit()
                refresh_session = None
            if not refresh_session:
                _logger.warning(f"Refresh attempt with invalidated or expired session: user_id={user_id}, session_id={session_id}")
                raise ApiError(AuthErrorCode.SESSION_EXPIRED)

        # Create new tokens bound to the same session
        access_token = AuthService.create_access_token(user.id, session_id=session_id)
        new_refresh_token = AuthService.create_refresh_token(user.id, session_id=session_id)

        _logger.info(f"Token refreshed for user {user.username}")

        auth_context = await load_auth_context(session, user)
        return build_login_response(auth_context, access_token, new_refresh_token)

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Token refresh error: {str(e)}")
        raise ApiError(ErrorCode.TOKEN_REFRESH_FAILED)


@router.post(
    AuthRoutes.LOGOUT.value,
    response_model=LogoutResponse,
    responses=problems(ErrorCode.NO_ACTIVE_SESSION, ErrorCode.SESSION_ALREADY_INVALIDATED, ErrorCode.LOGOUT_FAILED),
)
async def logout(
        request: Request,
        logout_request: LogoutRequest,
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """Logout user and invalidate session"""
    current_user = auth.user
    try:
        # The session of the access token; the body names one only for a token without a session
        session_id = auth.session_id or logout_request.session_id

        if not session_id:
            _logger.warning(f"No active session found for user {current_user.username}")
            raise ApiError(ErrorCode.NO_ACTIVE_SESSION)

        success = await AuthService.invalidate_session(
            session,
            session_id,
            current_user.id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent")
        )

        if not success:
            _logger.warning(f"Session already invalidated for user {current_user.username}")
            raise ApiError(ErrorCode.SESSION_ALREADY_INVALIDATED)

        _logger.info(f"User {current_user.username} logged out successfully")
        return LogoutResponse(message="Logged out successfully")

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Logout error: {str(e)}")
        raise ApiError(ErrorCode.LOGOUT_FAILED)


@router.get(AuthRoutes.ME.value, response_model=UserInfoResponse)
async def get_current_user_info(
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """Get current user information"""
    return build_user_info(auth)


@router.get(
    AuthRoutes.SESSIONS.value,
    response_model=UserSessionsResponse,
    responses=problems(ErrorCode.SESSIONS_LIST_FAILED),
)
async def get_user_sessions(
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """Get current user's active sessions"""
    current_user = auth.user
    try:
        from sqlmodel import select
        from datetime import datetime, timezone

        sessions_result = await session.exec(
            select(UserSession).where(
                UserSession.user_id == current_user.id,
                UserSession.is_active == True,
                UserSession.expires_at > datetime.now(timezone.utc)
            )
        )

        sessions = []
        for user_session in sessions_result.all():
            sessions.append(SessionInfoResponse(
                id=user_session.id,
                ip_address=user_session.ip_address,
                user_agent=user_session.user_agent,
                created_at=user_session.created_at.isoformat(),
                expires_at=user_session.expires_at.isoformat(),
                last_activity_at=user_session.last_activity.isoformat() if user_session.last_activity else None
            ))

        return UserSessionsResponse(sessions=sessions)

    except Exception as e:
        _logger.error(f"Get sessions error: {str(e)}")
        raise ApiError(ErrorCode.SESSIONS_LIST_FAILED)




@router.post(AuthRoutes.CHECK_PERMISSION.value, response_model=PermissionCheckResponse)
async def check_permission(
        check: PermissionCheckRequest,
        auth: AuthContext = Depends(require())
):
    """Evaluate a permission requirement for the current user without calling the guarded route"""
    result = Require.of(all=check.all, any=check.any, admin=check.admin).evaluate(auth)
    return PermissionCheckResponse(
        allowed=result.allowed,
        missing=list(result.missing),
        admin_required=result.admin_missing
    )


@router.get(AuthRoutes.ROUTE_ACCESS.value, response_model=RouteAccessResponse)
async def get_route_access(
        request: Request,
        auth: AuthContext = Depends(require()),
        version: PublishedVersion = Depends(request_version),
):
    """Routes of this API version the current user may call. Generated from route metadata; routes the user
    cannot call are not listed."""
    entries = [entry for entry in routes_allowed_for(route_access_map(request.app), auth) if version.owns(entry.path)]
    return RouteAccessResponse(routes=[
        RouteAccessEntryResponse(
            method=entry.method,
            path=entry.path,
            name=entry.name,
            tags=list(entry.tags),
            required=RequiredPermissions(**entry.require.as_dict())
        )
        for entry in entries
    ])
