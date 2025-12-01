from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, and_
import secrets

from app.backend.config import settings
from app.backend.utils.logging_config import get_auth_logger
from app.backend.models.user import User, UserSession
from app.backend.helpers.certeficates_helper import (
    validate_pki_certificate_data,
    hash_pki_certificate_data,
    create_pki_auth_audit_details
)
from app.backend.helpers.audit_helper import (
    create_audit_log,
    truncate_for_logging
)

# Get authentication logger
_logger = get_auth_logger()

_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12  # Higher security rounds
)


class AuthService:
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
                options={"leeway": settings.security.jwt_leeway_seconds}
            )
            
            if payload.get("type") != token_type:
                _logger.warning(f"Invalid token type: expected {token_type}, got {payload.get('type')}")
                return None
                
            return payload
        except JWTError as e:
            _logger.warning(f"Token verification failed: {str(e)}")
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

        # Create audit log using helper
        await create_audit_log(
            session=session,
            user_id=user.id,
            session_id=session_id,
            action="login",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True
        )

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
            # Validate certificate data format using helper
            if not await validate_pki_certificate_data(certificate_cn, certificate_serial, certificate_fingerprint):
                _logger.warning("Invalid PKI certificate data provided")
                return None

            _logger.debug(
                f"PKI authentication attempt - CN: {certificate_cn}, "
                f"Serial: {truncate_for_logging(certificate_serial, 8)}, "
                f"Fingerprint: {truncate_for_logging(certificate_fingerprint)}"
            )

            # Hash the certificate data for secure comparison using helper
            cn_hash, serial_hash, fingerprint_hash = await hash_pki_certificate_data(
                certificate_cn, certificate_serial, certificate_fingerprint
            )

            _logger.debug(
                f"Hashed PKI data - CN hash: {truncate_for_logging(cn_hash)}, "
                f"Serial hash: {truncate_for_logging(serial_hash)}, "
                f"Fingerprint hash: {truncate_for_logging(fingerprint_hash)}"
            )

            # Create audit log for authentication attempt using helper
            audit_details = await create_pki_auth_audit_details(
                certificate_cn, certificate_serial, fingerprint_hash
            )
            audit = await create_audit_log(
                session=session,
                action="pki_auth_attempt",
                resource_type="auth",
                success=False,
                ip_address=ip_address,
                details=audit_details
            )

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

            _logger.debug(f"PKI authentication query result: {'User found' if user else 'No matching user'}")

            if user:
                # Update audit log for success
                audit.success = True
                audit.user_id = user.id
                _logger.info(f"Successful PKI authentication for user {user.username}")
            else:
                _logger.warning(f"Failed PKI authentication attempt for CN: {certificate_cn}")

            await session.commit()
            return user

        except Exception as e:
            _logger.error(f"PKI authentication error: {str(e)}")
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

                # Create audit log using helper
                await create_audit_log(
                    session=session,
                    user_id=user_session.user_id,
                    session_id=session_id,
                    action="logout",
                    resource_type="session",
                    resource_id=int(session_id),
                    success=True
                )

                await session.commit()
                return True

        except Exception as e:
            _logger.error(f"Session invalidation error: {str(e)}")
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
                _logger.info(f"Cleaned up {count} expired sessions")

            return count

        except Exception as e:
            _logger.error(f"Session cleanup error: {str(e)}")
            await session.rollback()
            return 0

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt"""
        return _pwd_context.hash(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify password against hash"""
        return _pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def generate_secure_token(length: int = 32) -> str:
        """Generate cryptographically secure token"""
        return secrets.token_urlsafe(length)
