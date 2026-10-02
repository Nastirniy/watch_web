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
from app.backend.enums.errors import AuthErrorCode
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
    def create_access_token(user_id: int, session_id: Optional[str] = None, additional_claims: Optional[Dict[str, Any]] = None) -> str:
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
        
        if session_id:
            payload["sid"] = session_id
            
        if additional_claims:
            payload.update(additional_claims)
        
        return jwt.encode(payload, settings.security.secret_key, algorithm=settings.security.algorithm)

    @staticmethod
    def create_refresh_token(user_id: int, session_id: Optional[str] = None) -> str:
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
        
        if session_id:
            payload["sid"] = session_id
        
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
            user_agent: Optional[str] = None,
            request_url: Optional[str] = None
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
            request_url=request_url,
            details={"message": f"User {user.username} logged in"},
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
    ) -> tuple[Optional[User], Optional[str]]:
        """
        Authenticate user by PKI certificate with enhanced security.

        Returns:
            (user, None) on success
            (None, error_code) on failure -- error_code is an AuthErrorCode value
        """
        if not await validate_pki_certificate_data(certificate_cn, certificate_serial, certificate_fingerprint):
            _logger.warning("Invalid PKI certificate data provided")
            return None, AuthErrorCode.CERTIFICATE_DATA_MISSING

        _logger.debug(
            f"PKI authentication attempt - CN: {certificate_cn}, "
            f"Serial: {truncate_for_logging(certificate_serial, 8)}, "
            f"Fingerprint: {truncate_for_logging(certificate_fingerprint)}"
        )

        # Hash the certificate data for secure comparison using helper
        cn_hash, serial_hash, fingerprint_hash = await hash_pki_certificate_data(
            certificate_cn, certificate_serial, certificate_fingerprint
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

        # Find user by certificate (without status filters first)
        result = await session.exec(
            select(User).where(
                and_(
                    User.pki_certificate_cn_hash == cn_hash,
                    User.pki_certificate_serial_hash == serial_hash,
                    User.pki_certificate_fingerprint_hash == fingerprint_hash,
                )
            )
        )
        user = result.first()

        if not user:
            _logger.warning(f"No user found for certificate CN: {certificate_cn}")
            await session.commit()
            return None, AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED

        # Check account status separately for distinct error codes
        if user.is_deleted:
            _logger.warning(f"Deleted user attempted login: user_id={user.id}, username={user.username}")
            audit.user_id = user.id
            await session.commit()
            return None, AuthErrorCode.ACCOUNT_DELETED

        if not user.is_active:
            _logger.warning(f"Deactivated user attempted login: user_id={user.id}, username={user.username}")
            audit.user_id = user.id
            await session.commit()
            return None, AuthErrorCode.ACCOUNT_DEACTIVATED

        # Success
        audit.success = True
        audit.user_id = user.id
        _logger.info(f"Successful PKI authentication for user {user.username}")

        await session.commit()
        return user, None

    @staticmethod
    async def invalidate_session(
            session: AsyncSession,
            session_id: str,
            user_id: Optional[int] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None
    ) -> bool:
        """Invalidate a user session"""
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
                ip_address=ip_address,
                user_agent=user_agent,
                details={"message": "User session invalidated (logout)"},
                success=True
            )

            await session.commit()
            return True

        return False

    @staticmethod
    async def invalidate_all_user_sessions(
            session: AsyncSession,
            user_id: int,
            reason: str = "account_deactivated",
            actor_user_id: Optional[int] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            commit: bool = True
    ) -> int:
        """Invalidate all active sessions for a user (e.g. on deactivation or soft-delete).

        commit=False leaves the changes in the caller's transaction so that they land together with
        the change that caused them (the caller commits). Database errors propagate either way.
        """
        result = await session.exec(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.is_active == True
            )
        )
        active_sessions = result.all()

        count = 0
        now = datetime.now(timezone.utc)
        for user_session in active_sessions:
            user_session.is_active = False
            user_session.ended_at = now
            session.add(user_session)
            count += 1

        if count > 0:
            await create_audit_log(
                session=session,
                user_id=actor_user_id or user_id,
                action="force_logout",
                resource_type="session",
                resource_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={
                    "message": f"All {count} active sessions invalidated for user_id={user_id}",
                    "reason": reason
                },
                success=True
            )
            if commit:
                await session.commit()
            _logger.info(f"Invalidated {count} active sessions for user_id={user_id}, reason={reason}")

        return count

    @staticmethod
    async def expire_sessions_of_inactive_user(session: AsyncSession, user: User) -> int:
        """End every live session of a user whose account is deactivated or archived.

        Called from the request path when such a user presents a token: the sessions were normally ended
        when the flag changed, but a flag flipped outside the API (direct database change, restore of a
        backup) would leave them live and count towards presence. Returns the number of sessions ended.
        """
        if user.is_active and not user.is_deleted:
            return 0
        reason = "account_deleted" if user.is_deleted else "account_deactivated"
        return await AuthService.invalidate_all_user_sessions(session, user_id=user.id, reason=reason)

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
