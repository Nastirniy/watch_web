from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.auth import AuthService
from app.backend.services.permissions import PermissionService
from app.backend.dependencies import get_current_user
from app.backend.models.user import User, UserSession
from app.backend.enums.routes import AuthRoutes
from app.backend.enums.general import Permissions, AppEnvironment
from app.backend.utils.logging_config import get_auth_logger
from app.backend.config import settings
from app.backend.helpers.certeficates_helper import parse_cn
from app.backend.routes.models.request.auth_request import (
    LoginRequest,
    RefreshRequest,
    LogoutRequest
)
from app.backend.routes.models.response.auth_response import (
    LoginResponse,
    LogoutResponse,
    UserInfoResponse,
    SessionInfoResponse,
    UserSessionsResponse
)

router = APIRouter()
_logger = get_auth_logger()


@router.post(AuthRoutes.LOGIN.value, response_model=LoginResponse)
async def login(
        request: Request,
        login_data: Optional[LoginRequest] = None,
        session: AsyncSession = Depends(get_session)
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
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="JSON authentication data required in development mode"
                )

        else:
            # Production mode: extract SSL certificate data from headers
            ssl_verify = request.headers.get("x-ssl-client-verify")
            ssl_dn = request.headers.get("x-ssl-client-dn")
            ssl_fingerprint = request.headers.get("x-ssl-client-fingerprint")
            ssl_serial = request.headers.get("x-ssl-client-serial")
            ssl_issuer = request.headers.get("x-ssl-issuer-dn")

            # Verify that certificate was validated by nginx
            if ssl_verify != "SUCCESS":
                _logger.warning(
                    f"Certificate verification failed from IP: {client_ip}, "
                    f"status: {ssl_verify}"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Certificate verification failed",
                    headers={"WWW-Authenticate": "Bearer"}
                )

            # Parse CN from Distinguished Name
            cn = await parse_cn(ssl_dn)

            # Validate required certificate data
            if not cn or not ssl_serial or not ssl_fingerprint:
                _logger.warning(
                    f"Missing certificate data from IP: {client_ip}, "
                    f"CN: {cn}, Serial: {ssl_serial}, Fingerprint: {ssl_fingerprint}"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid certificate data",
                    headers={"WWW-Authenticate": "Bearer"}
                )

        # Authenticate with enhanced PKI method
        user = await AuthService.authenticate_pki(
            session,
            certificate_cn=cn,
            certificate_serial=ssl_serial,
            certificate_fingerprint=ssl_fingerprint,
            ip_address=client_ip
        )
        if not user:
            _logger.warning(
                f"Failed login attempt from IP: {client_ip}, "
                f"CN: {cn}, Serial: {ssl_serial}"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid certificate credentials",
                headers={"WWW-Authenticate": "Bearer"}
            )

        # Create session
        user_session = await AuthService.create_session(
            session,
            user,
            ip_address=client_ip,
            user_agent=user_agent
        )

        # Create both access and refresh tokens
        access_token = AuthService.create_access_token(user.id)
        refresh_token = AuthService.create_refresh_token(user.id)

        _logger.info(
            f"Successful login for user {user.username} from IP: {client_ip}, "
            f"CN: {cn}, Serial: {ssl_serial}"
        )

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.security.access_token_expire_minutes * 60,
            user_id=user.id,
            username=user.username
        )

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Login error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service unavailable"
        )

#
# @router.post(AuthRoutes.LOGIN.value, response_model=LoginResponse)
# async def login(
#         request: Request,
#         credentials: LoginRequest,
#         session: AsyncSession = Depends(get_session)
# ):
#     """Authenticate user with PKI certificate using enhanced security"""
#     try:
#         client_ip = request.client.host if request.client else None
#         user_agent = request.headers.get("user-agent")
#
#         # Authenticate with enhanced PKI method
#         user = await AuthService.authenticate_pki(
#             session,
#             credentials.certificate_cn,
#             credentials.certificate_serial,
#             credentials.certificate_fingerprint,
#             ip_address=client_ip
#         )
#
#         if not user:
#             logger.warning(f"Failed login attempt from IP: {client_ip}")
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Invalid certificate credentials",
#                 headers={"WWW-Authenticate": "Bearer"}
#             )
#
#         # Create session
#         user_session = await AuthService.create_session(
#             session,
#             user,
#             ip_address=client_ip,
#             user_agent=user_agent
#         )
#
#         # Create both access and refresh tokens
#         access_token = AuthService.create_access_token(user.id)
#         refresh_token = AuthService.create_refresh_token(user.id)
#
#         logger.info(f"Successful login for user {user.username} from IP: {client_ip}")
#
#         return LoginResponse(
#             access_token=access_token,
#             refresh_token=refresh_token,
#             expires_in=settings.security.access_token_expire_minutes * 60,
#             user_id=user.id,
#             username=user.username
#         )
#
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Login error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Authentication service unavailable"
#         )


@router.post(AuthRoutes.REFRESH.value, response_model=LoginResponse)
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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token"
            )

        user_id = int(payload.get("sub"))

        # Verify user still exists and is active
        from sqlmodel import select
        user_result = await session.exec(
            select(User).where(
                User.id == user_id,
                User.is_active == True,
                User.is_deleted == False
            )
        )
        user = user_result.first()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive"
            )

        # Create new tokens
        access_token = AuthService.create_access_token(user.id)
        new_refresh_token = AuthService.create_refresh_token(user.id)

        _logger.info(f"Token refreshed for user {user.username}")

        return LoginResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=settings.security.access_token_expire_minutes * 60,
            user_id=user.id,
            username=user.username
        )

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Token refresh error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token refresh failed"
        )


@router.post(AuthRoutes.LOGOUT.value, response_model=LogoutResponse)
async def logout(
        request: Request,
        logout_request: LogoutRequest,
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
):
    """Logout user and invalidate session"""
    try:
        session_id = getattr(request.state, 'session_id', logout_request.session_id)

        if not session_id:
            _logger.warning(f"No active session found for user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active session found"
            )

        success = await AuthService.invalidate_session(
            session,
            session_id,
            current_user.id
        )

        if not success:
            _logger.warning(f"Session already invalidated for user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Session already invalidated"
            )

        _logger.info(f"User {current_user.username} logged out successfully")
        return LogoutResponse(message="Logged out successfully")

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Logout error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed"
        )


@router.get(AuthRoutes.ME.value, response_model=UserInfoResponse)
async def get_current_user_info(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
):
    """Get current user information"""

    # Check if user has admin permission
    is_admin = await PermissionService.has_permission(
        session,
        current_user,
        Permissions.IS_ADMIN.value
    )

    return UserInfoResponse(
        id=current_user.id,
        username=current_user.username,
        username_alias=current_user.username_alias,
        post_id=current_user.post_id,
        is_active=current_user.is_active,
        is_admin=is_admin,
        last_login_at=current_user.last_login_at.isoformat() if current_user.last_login_at else None
    )


@router.get(AuthRoutes.SESSIONS.value, response_model=UserSessionsResponse)
async def get_user_sessions(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
):
    """Get current user's active sessions"""
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve sessions"
        )


# @router.get("/test-cert")
# async def test_cert(request: Request):
#     """
#     Тестовий endpoint для перевірки клієнтського сертифіката.
#     Повертає інформацію про сертифікат із заголовків, переданих NGINX.
#     """
#     ssl_verify = request.headers.get("x-ssl-client-verify")
#     ssl_dn = request.headers.get("x-ssl-client-dn")
#     ssl_fingerprint = request.headers.get("x-ssl-client-fingerprint")
#     ssl_serial = request.headers.get("x-ssl-client-serial")
#     ssl_issuer = request.headers.get("x-ssl-issuer-dn")
#
#     cn = await parse_cn(ssl_dn)
#
#     print({
#         "ssl_verify": ssl_verify,
#         "ssl_dn": ssl_dn,
#         "ssl_cn": cn,
#         "ssl_fingerprint": ssl_fingerprint,
#         "ssl_serial": ssl_serial,
#         "ssl_issuer": ssl_issuer
#     })
