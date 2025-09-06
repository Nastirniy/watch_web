from typing import Optional
from datetime import datetime, timedelta, timezone
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from loguru import logger

from app.backend.config import settings
from app.backend.models.user import User, UserSession
from app.backend.models.audit import AuditLog
import secrets

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    @staticmethod
    def create_access_token(user_id: int) -> str:
        """Create JWT access token"""
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.security.access_token_expire_minutes)
        payload = {
            "sub": str(user_id),
            "exp": expire,
            "type": "access"
        }
        return jwt.encode(payload, settings.security.secret_key, algorithm=settings.security.algorithm)

    @staticmethod
    async def create_session(
            session: AsyncSession,
            user: User,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None
    ) -> UserSession:
        """Create user session"""
        session_id = secrets.token_urlsafe(64)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.security.session_expire_hours)

        user_session = UserSession(
            id=session_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=expires_at
        )
        session.add(user_session)

        # Update last login
        user.last_login_at = datetime.now(timezone.utc)
        session.add(user)

        # Create audit log
        audit = AuditLog(
            user_id=user.id,
            session_id=session_id,
            action="login",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True
        )
        session.add(audit)

        await session.commit()
        return user_session

    @staticmethod
    async def authenticate_pki(
            session: AsyncSession,
            certificate_cn: str,
            certificate_serial: str,
            certificate_fingerprint: str
    ) -> Optional[User]:
        """Authenticate user by PKI certificate"""
        result = await session.exec(
            select(User).where(
                User.pki_certificate_cn == certificate_cn,
                User.pki_certificate_serial == certificate_serial,
                User.pki_certificate_fingerprint == certificate_fingerprint,
                User.is_active == True,
                User.is_deleted == False
            )
        )
        return result.first()