from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_
import secrets
import hashlib
import hmac

from app.backend.config import settings
from app.backend.utils.logging_config import get_auth_logger
from app.backend.models.user import User, UserSession
from app.backend.models.audit import AuditLog

# Get authentication logger
logger = get_auth_logger()

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12  # Higher security rounds
)


class AuthService:
    @staticmethod
    def hash_pki_data(data: str) -> str:
        """Hash PKI certificate data using SHA256"""
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    
    @staticmethod
    def create_access_token(user_id: int, additional_claims: Optional[Dict[str, Any]] = None) -> str:
        """Create JWT access token with enhanced security"""
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=settings.security.access_token_expire_minutes)
        
        payload = {
            "sub": str(user_id),
            "exp": expire,
            "iat": now,
            "nbf": now,  # Not before
            "type": "access",
            "jti": secrets.token_urlsafe(32)  # JWT ID for token revocation
        }
        
        if additional_claims:
            payload.update(additional_claims)
        
        return jwt.encode(payload, settings.security.secret_key, algorithm=settings.security.algorithm)

    @staticmethod
    def create_refresh_token(user_id: int) -> str:
        """Create JWT refresh token"""
        now = datetime.now(timezone.utc)
        expire = now + timedelta(days=settings.security.refresh_token_expire_days)
        
        payload = {
            "sub": str(user_id),
            "exp": expire,
            "iat": now,
            "nbf": now,
            "type": "refresh",
            "jti": secrets.token_urlsafe(32)
        }
        
        return jwt.encode(payload, settings.security.secret_key, algorithm=settings.security.algorithm)

    @staticmethod
    def verify_token(token: str, token_type: str = "access") -> Optional[Dict[str, Any]]:
        """Verify and decode JWT token"""
        try:
            payload = jwt.decode(
                token,
                settings.security.secret_key,
                algorithms=[settings.security.algorithm],
                leeway=settings.security.jwt_leeway_seconds
            )
            
            if payload.get("type") != token_type:
                logger.warning(f"Invalid token type: expected {token_type}, got {payload.get('type')}")
                return None
                
            return payload
        except JWTError as e:
            logger.warning(f"Token verification failed: {str(e)}")
            return None

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
            certificate_fingerprint: str,
            ip_address: Optional[str] = None
    ) -> Optional[User]:
        """Authenticate user by PKI certificate with enhanced security"""
        try:
            # Validate certificate data format
            if not all([certificate_cn, certificate_serial, certificate_fingerprint]):
                logger.warning("Invalid PKI certificate data provided")
                return None

            # Hash the certificate data for secure comparison
            cn_hash = AuthService.hash_pki_data(certificate_cn)
            serial_hash = AuthService.hash_pki_data(certificate_serial)
            fingerprint_hash = AuthService.hash_pki_data(certificate_fingerprint)

            # Create audit log for authentication attempt
            audit = AuditLog(
                user_id=None,
                session_id=None,
                action="pki_auth_attempt",
                resource_type="auth",
                resource_id=None,
                ip_address=ip_address,
                success=False,
                details={
                    "certificate_cn": certificate_cn,
                    "certificate_serial": certificate_serial,
                    # Don't log full fingerprint for security
                    "certificate_fingerprint_hash": fingerprint_hash[:16]
                }
            )
            session.add(audit)

            # Find user with matching hashed PKI credentials
            result = await session.exec(
                select(User).where(
                    and_(
                        User.pki_certificate_cn_hash == cn_hash,
                        User.pki_certificate_serial_hash == serial_hash,
                        User.pki_certificate_fingerprint_hash == fingerprint_hash,
                        User.is_active == True,
                        User.is_deleted == False
                    )
                )
            )
            user = result.first()

            if user:
                # Update audit log for success
                audit.success = True
                audit.user_id = user.id
                logger.info(f"Successful PKI authentication for user {user.username}")
            else:
                logger.warning(f"Failed PKI authentication attempt for CN: {certificate_cn}")

            await session.commit()
            return user

        except Exception as e:
            logger.error(f"PKI authentication error: {str(e)}")
            await session.rollback()
            return None

    @staticmethod
    async def invalidate_session(
            session: AsyncSession,
            session_id: str,
            user_id: Optional[int] = None
    ) -> bool:
        """Invalidate a user session"""
        try:
            # Find and deactivate session
            query = select(UserSession).where(UserSession.id == session_id)
            if user_id:
                query = query.where(UserSession.user_id == user_id)
            
            result = await session.exec(query)
            user_session = result.first()

            if user_session:
                user_session.is_active = False
                user_session.ended_at = datetime.now(timezone.utc)
                session.add(user_session)

                # Create audit log
                audit = AuditLog(
                    user_id=user_session.user_id,
                    session_id=session_id,
                    action="logout",
                    resource_type="session",
                    resource_id=session_id,
                    success=True
                )
                session.add(audit)

                await session.commit()
                return True

        except Exception as e:
            logger.error(f"Session invalidation error: {str(e)}")
            await session.rollback()

        return False

    @staticmethod
    async def cleanup_expired_sessions(session: AsyncSession) -> int:
        """Clean up expired sessions"""
        try:
            expired_sessions = await session.exec(
                select(UserSession).where(
                    UserSession.expires_at < datetime.now(timezone.utc),
                    UserSession.is_active == True
                )
            )
            
            count = 0
            for user_session in expired_sessions.all():
                user_session.is_active = False
                user_session.ended_at = datetime.now(timezone.utc)
                session.add(user_session)
                count += 1

            if count > 0:
                await session.commit()
                logger.info(f"Cleaned up {count} expired sessions")

            return count

        except Exception as e:
            logger.error(f"Session cleanup error: {str(e)}")
            await session.rollback()
            return 0

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt"""
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify password against hash"""
        return pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def generate_secure_token(length: int = 32) -> str:
        """Generate cryptographically secure token"""
        return secrets.token_urlsafe(length)