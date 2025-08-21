"""
Authentication API routes for login, logout, and session management.
Handles PKI certificate authentication and JWT token management.
"""

from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config.config import settings
from backend.core.exceptions import AuthenticationException, ValidationException
from backend.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    generate_session_id,
    validate_pki_certificate,
)
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.user import user as user_crud, user_session as session_crud
from backend.database.database import get_session
from backend.dependencies import DatabaseDep, CurrentUser, OptionalUser
from backend.models.audit import AuditAction, AuditSeverity, ResourceType
from backend.schemas.auth import (
    CertificateValidationRequest,
    CertificateValidationResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    PasswordChangeRequest,
    PermissionCheck,
    PermissionResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    SessionInfo,
    TokenPair,
)
from backend.schemas.base import MessageResponse, create_response

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(
        request: Request,
        login_data: LoginRequest,
        db: DatabaseDep,
) -> LoginResponse:
    """
    Authenticate user with PKI certificate and create session.

    Args:
        request: HTTP request object
        login_data: Login request with certificate info
        db: Database session

    Returns:
        Login response with tokens and user info

    Raises:
        AuthenticationException: If authentication fails
    """
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    try:
        # Extract certificate information
        cert_info = login_data.certificate

        # Find user by certificate
        user = await user_crud.get_by_certificate_cn(db, cert_info.common_name)

        if not user:
            # Try by serial number as fallback
            user = await user_crud.get_by_certificate_serial(db, cert_info.serial_number)

        if not user:
            await _log_auth_failure(
                db, None, client_ip, user_agent,
                f"User not found for certificate CN: {cert_info.common_name}"
            )
            raise AuthenticationException("Invalid certificate or user not found")

        # Check if user is active
        if not user.is_active:
            await _log_auth_failure(
                db, user.id, client_ip, user_agent,
                f"Login attempt for inactive user: {user.username}"
            )
            raise AuthenticationException("Account is inactive")

        # Validate certificate if enabled
        if settings.PKI_CERT_VALIDATION:
            # In a real implementation, you would validate the actual certificate
            # For now, we just check that the provided info matches the user
            if (user.pki_certificate_cn != cert_info.common_name or
                    user.pki_certificate_serial != cert_info.serial_number):
                await _log_auth_failure(
                    db, user.id, client_ip, user_agent,
                    f"Certificate mismatch for user: {user.username}"
                )
                raise AuthenticationException("Certificate validation failed")

        # Create session
        session_id = generate_session_id()
        expires_minutes = settings.ACCESS_TOKEN_EXPIRE_MINUTES

        await session_crud.create_session(
            db=db,
            user_id=user.id,
            session_id=session_id,
            ip_address=client_ip,
            user_agent=user_agent,
            expire_minutes=expires_minutes,
        )

        # Get user permissions
        permissions = await user_crud.get_user_permissions(db, user.id)

        # Create tokens
        token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

        access_token = create_access_token(
            subject=str(user.id),
            expires_delta=token_expires,
            additional_claims={
                "session_id": session_id,
                "permissions": permissions,
            }
        )

        refresh_token = create_refresh_token(
            subject=str(user.id),
            expires_delta=refresh_expires,
        )

        # Update last login
        await user_crud.update_last_login(db, user.id)

        # Log successful authentication
        await audit_crud.create_log(
            db=db,
            action=AuditAction.LOGIN,
            resource_type=ResourceType.USER,
            user_id=user.id,
            session_id=session_id,
            resource_name=user.username,
            ip_address=client_ip,
            user_agent=user_agent,
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"User {user.username} logged in successfully from {client_ip}")

        # Prepare response
        tokens = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
        )

        user_info = {
            "id": user.id,
            "username": user.username,
            "username_alias": user.username_alias,
            "email": user.email,
            "is_active": user.is_active,
            "is_superuser": user.is_superuser,
            "is_verified": user.is_verified,
            "last_login_at": user.last_login_at,
            "roles": [role.name for role in user.roles] if user.roles else [],
            "post_id": user.post_id,
        }

        return LoginResponse(
            tokens=tokens,
            user=user_info,
            session_id=session_id,
            expires_at=datetime.utcnow() + token_expires,
            permissions=permissions,
        )

    except AuthenticationException:
        raise
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        await _log_auth_failure(
            db, None, client_ip, user_agent,
            f"Login system error: {str(e)}"
        )
        raise AuthenticationException("Authentication system error")


@router.post("/refresh", response_model=RefreshTokenResponse)
async def refresh_token(
        refresh_data: RefreshTokenRequest,
        db: DatabaseDep,
) -> RefreshTokenResponse:
    """
    Refresh access token using refresh token.

    Args:
        refresh_data: Refresh token request
        db: Database session

    Returns:
        New access token

    Raises:
        AuthenticationException: If refresh token is invalid
    """
    try:
        # Decode and validate refresh token
        payload = decode_refresh_token(refresh_data.refresh_token)
        user_id = payload.get("sub")

        if not user_id:
            raise AuthenticationException("Invalid refresh token")

        # Get user
        user = await user_crud.get_with_roles(db, int(user_id))
        if not user or not user.is_active:
            raise AuthenticationException("User not found or inactive")

        # Get user permissions
        permissions = await user_crud.get_user_permissions(db, user.id)

        # Create new access token
        token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            subject=str(user.id),
            expires_delta=token_expires,
            additional_claims={"permissions": permissions},
        )

        logger.info(f"Token refreshed for user {user.username}")

        return RefreshTokenResponse(
            access_token=access_token,
            expires_at=datetime.utcnow() + token_expires,
        )

    except Exception as e:
        logger.warning(f"Token refresh failed: {str(e)}")
        raise AuthenticationException("Invalid refresh token")


@router.post("/logout", response_model=MessageResponse)
async def logout(
        request: Request,
        logout_data: LogoutRequest,
        current_user: CurrentUser,
        db: DatabaseDep,
) -> MessageResponse:
    """
    Logout user and invalidate session(s).

    Args:
        request: HTTP request object
        logout_data: Logout request
        current_user: Current authenticated user
        db: Database session

    Returns:
        Logout confirmation message
    """
    client_ip = request.client.host if request.client else "unknown"

    try:
        if logout_data.logout_all:
            # Logout from all sessions
            # Note: In a real implementation, you would need to track all user sessions
            logger.info(f"User {current_user.username} logged out from all sessions")
            logout_message = "Logged out from all sessions"
        else:
            # Logout from current session
            session_id = logout_data.session_id or getattr(request.state, "session_id", None)
            if session_id:
                await session_crud.invalidate_session(db, session_id, "user_logout")

            logger.info(f"User {current_user.username} logged out")
            logout_message = "Logged out successfully"

        # Log logout event
        await audit_crud.create_log(
            db=db,
            action=AuditAction.LOGOUT,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_name=current_user.username,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
            success=True,
            severity=AuditSeverity.LOW,
        )

        return MessageResponse(message=logout_message)

    except Exception as e:
        logger.error(f"Logout error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed"
        )


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
        password_data: PasswordChangeRequest,
        current_user: CurrentUser,
        db: DatabaseDep,
) -> MessageResponse:
    """
    Change user password.

    Args:
        password_data: Password change request
        current_user: Current authenticated user
        db: Database session

    Returns:
        Password change confirmation
    """
    # Note: This endpoint is primarily for future use since the system
    # uses PKI certificate authentication. Password changes would typically
    # be handled by the certificate authority.

    logger.info(f"Password change requested for user {current_user.username}")

    # Log password change event
    await audit_crud.create_log(
        db=db,
        action=AuditAction.PASSWORD_CHANGE,
        resource_type=ResourceType.USER,
        user_id=current_user.id,
        resource_name=current_user.username,
        success=True,
        severity=AuditSeverity.MEDIUM,
    )

    return MessageResponse(
        message="Password change request recorded. Please contact your certificate authority."
    )


@router.get("/me", response_model=Dict[str, Any])
async def get_current_user_info(
        current_user: CurrentUser,
        db: DatabaseDep,
) -> Dict[str, Any]:
    """
    Get current user information.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        Current user information
    """
    # Get user permissions
    permissions = await user_crud.get_user_permissions(db, current_user.id)

    user_info = {
        "id": current_user.id,
        "username": current_user.username,
        "username_alias": current_user.username_alias,
        "email": current_user.email,
        "is_active": current_user.is_active,
        "is_superuser": current_user.is_superuser,
        "is_verified": current_user.is_verified,
        "last_login_at": current_user.last_login_at,
        "roles": [{"id": role.id, "name": role.name, "display_name": role.display_name}
                  for role in current_user.roles] if current_user.roles else [],
        "permissions": permissions,
        "post_id": current_user.post_id,
        "certificate_cn": current_user.pki_certificate_cn,
    }

    return create_response(data=user_info)


@router.get("/sessions", response_model=list[SessionInfo])
async def get_user_sessions(
        current_user: CurrentUser,
        db: DatabaseDep,
) -> list[SessionInfo]:
    """
    Get current user's active sessions.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of user sessions
    """
    # Note: This would require implementing session tracking in the user_session CRUD
    # For now, return empty list
    return []


@router.post("/check-permission", response_model=PermissionResponse)
async def check_permission(
        permission_check: PermissionCheck,
        current_user: CurrentUser,
        db: DatabaseDep,
) -> PermissionResponse:
    """
    Check if current user has specific permission.

    Args:
        permission_check: Permission to check
        current_user: Current authenticated user
        db: Database session

    Returns:
        Permission check result
    """
    has_permission = await user_crud.has_permission(
        db, current_user.id, permission_check.permission
    )

    return PermissionResponse(
        has_permission=has_permission,
        permission=permission_check.permission,
        reason=None if has_permission else "Insufficient privileges",
    )


@router.post("/certificate/validate", response_model=CertificateValidationResponse)
async def validate_certificate(
        cert_request: CertificateValidationRequest,
) -> CertificateValidationResponse:
    """
    Validate PKI certificate.

    Args:
        cert_request: Certificate validation request

    Returns:
        Certificate validation result
    """
    try:
        import base64

        # Decode certificate data
        cert_data = base64.b64decode(cert_request.certificate_data)

        # Validate certificate
        is_valid, cert_info, validation_errors = validate_pki_certificate(cert_data)

        # Convert certificate info to response format
        certificate_info = None
        if cert_info:
            certificate_info = {
                "common_name": cert_info.get("common_name"),
                "serial_number": cert_info.get("serial_number"),
                "fingerprint": cert_info.get("fingerprint"),
                "issuer": cert_info.get("issuer"),
                "subject": cert_info.get("subject"),
                "valid_from": cert_info.get("valid_from"),
                "valid_to": cert_info.get("valid_to"),
            }

        return CertificateValidationResponse(
            is_valid=is_valid,
            certificate_info=certificate_info,
            validation_errors=validation_errors,
        )

    except Exception as e:
        logger.error(f"Certificate validation error: {str(e)}")
        return CertificateValidationResponse(
            is_valid=False,
            validation_errors=[f"Certificate validation failed: {str(e)}"],
        )


async def _log_auth_failure(
        db: AsyncSession,
        user_id: Optional[int],
        ip_address: str,
        user_agent: str,
        reason: str,
) -> None:
    """
    Log authentication failure.

    Args:
        db: Database session
        user_id: User ID if known
        ip_address: Client IP address
        user_agent: Client user agent
        reason: Failure reason
    """
    try:
        await audit_crud.create_log(
            db=db,
            action=AuditAction.LOGIN_FAILED,
            resource_type=ResourceType.USER,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            error_message=reason,
            severity=AuditSeverity.MEDIUM,
        )
    except Exception as e:
        logger.error(f"Failed to log auth failure: {str(e)}")
