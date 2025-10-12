from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from pydantic import BaseModel, Field, field_validator
import re

from app.backend.database import get_session
from app.backend.services.auth import AuthService
from app.backend.dependencies import get_current_user
from app.backend.models.user import User, UserSession
from app.backend.enums.routes import AuthRoutes
from app.backend.utils.logging_config import get_auth_logger
from app.backend.config import settings

router = APIRouter()
logger = get_auth_logger()


class LoginRequest(BaseModel):
    certificate_cn: str = Field(..., min_length=1, max_length=255)
    certificate_serial: str = Field(..., min_length=1, max_length=255)
    certificate_fingerprint: str = Field(..., min_length=1, max_length=512)

    @field_validator('certificate_cn')
    @classmethod
    def validate_cn(cls, v: str) -> str:
        # Allow alphanumeric, spaces, dots, dashes, underscores, equals, and @ symbols for email-like CNs
        if not re.match(r'^[a-zA-Z0-9\s\-_.=@]+$', v):
            raise ValueError('Invalid certificate CN format')
        return v.strip()

    @field_validator('certificate_serial')
    @classmethod
    def validate_serial(cls, v: str) -> str:
        # Allow alphanumeric characters, dashes and underscores for serials
        if not re.match(r'^[a-fA-F0-9\-_]+$', v):
            raise ValueError('Invalid certificate serial format')
        return v.strip()

    @field_validator('certificate_fingerprint')
    @classmethod
    def validate_fingerprint(cls, v: str) -> str:
        # Allow hex characters, colons, and dashes for fingerprints
        if not re.match(r'^[a-fA-F0-9:\-]+$', v):
            raise ValueError('Invalid certificate fingerprint format')
        return v.strip()


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LogoutRequest(BaseModel):
    session_id: Optional[str] = None


@router.post(AuthRoutes.LOGIN.value, response_model=LoginResponse)
async def login(
        request: Request,
        credentials: LoginRequest,
        session: AsyncSession = Depends(get_session)
):
    """Authenticate user with PKI certificate using enhanced security"""
    try:
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        # Authenticate with enhanced PKI method
        user = await AuthService.authenticate_pki(
            session,
            credentials.certificate_cn,
            credentials.certificate_serial,
            credentials.certificate_fingerprint,
            ip_address=client_ip
        )

        if not user:
            logger.warning(f"Failed login attempt from IP: {client_ip}")
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

        logger.info(f"Successful login for user {user.username} from IP: {client_ip}")

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
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service unavailable"
        )


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
                User.is_deleted is False
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

        logger.info(f"Token refreshed for user {user.username}")

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
        logger.error(f"Token refresh error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token refresh failed"
        )


@router.post(AuthRoutes.LOGOUT.value)
async def logout(
        request: Request,
        logout_request: LogoutRequest,
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
):
    """Logout user and invalidate session"""
    try:
        session_id = getattr(request.state, 'session_id', logout_request.session_id)
        
        if session_id:
            success = await AuthService.invalidate_session(
                session, 
                session_id, 
                current_user.id
            )
            if success:
                logger.info(f"User {current_user.username} logged out successfully")
                return {"message": "Logged out successfully"}
            else:
                return {"message": "Session already invalidated"}
        else:
            return {"message": "No active session found"}

    except Exception as e:
        logger.error(f"Logout error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed"
        )


@router.get(AuthRoutes.ME.value)
async def get_current_user_info(
        current_user: User = Depends(get_current_user)
):
    """Get current user information"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "username_alias": current_user.username_alias,
        "post_id": current_user.post_id,
        "is_active": current_user.is_active,
        "last_login_at": current_user.last_login_at
    }


@router.get(AuthRoutes.SESSIONS.value)
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
            sessions.append({
                "id": user_session.id,
                "ip_address": user_session.ip_address,
                "user_agent": user_session.user_agent,
                "created_at": user_session.created_at,
                "expires_at": user_session.expires_at,
                "last_activity_at": user_session.last_activity_at
            })

        return {"sessions": sessions}

    except Exception as e:
        logger.error(f"Get sessions error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve sessions"
        )

if __name__ == "__main__":
    from fastapi import FastAPI, Request

    app = FastAPI(title="Test mTLS Endpoint")

    def parse_cn(dn: str) -> str | None:
        """Парсить CN із Distinguished Name"""
        if not dn:
            return None
        parts = [p.strip() for p in dn.split(",")]
        for p in parts:
            if p.startswith("CN="):
                return p[3:]
        return None

    @router.get("/test-cert")
    async def test_cert(request: Request):
        """
        Тестовий endpoint для перевірки клієнтського сертифіката.
        Повертає інформацію про сертифікат із заголовків, переданих NGINX.
        """
        ssl_verify = request.headers.get("x-ssl-client-verify")
        ssl_dn = request.headers.get("x-ssl-client-dn")
        ssl_fingerprint = request.headers.get("x-ssl-client-fingerprint")
        ssl_serial = request.headers.get("x-ssl-client-serial")
        ssl_issuer = request.headers.get("x-ssl-issuer-dn")

        cn = parse_cn(ssl_dn)

        print({
            "ssl_verify": ssl_verify,
            "ssl_dn": ssl_dn,
            "ssl_cn": cn,
            "ssl_fingerprint": ssl_fingerprint,
            "ssl_serial": ssl_serial,
            "ssl_issuer": ssl_issuer
        })
