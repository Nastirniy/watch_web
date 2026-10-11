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
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.certificates import (
    validate_pki_certificate_data,
    hash_pki_certificate_data,
    serial_hashes,
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
    def create_access_token(user_id: int, session_id: Optional[str] = None, additional_claims: Optional[Dict[str, Any]] = None,
                            not_after: Optional[datetime] = None) -> str:
        """Create JWT access token; it never outlives not_after (the end of its session)"""
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=settings.security.access_token_expire_minutes)
        if not_after is not None:
            expire = min(expire, session_lifetimes.utc(not_after))
        
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
    def create_refresh_token(user_id: int, session_id: Optional[str] = None, expires_at: Optional[datetime] = None) -> str:
        """Create JWT refresh token (session_tokens sets expires_at; the session is checked at every refresh anyway)"""
        now = datetime.now(timezone.utc)
        expire = session_lifetimes.utc(expires_at) if expires_at is not None else now + AuthService.refresh_window()
        
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
    def refresh_window() -> timedelta:
        """How long a refresh token stays usable: the client may use its access token to the end and then still
        refresh, as long as the session has not ended by inactivity in between"""
        return timedelta(minutes=settings.security.access_token_expire_minutes) + session_lifetimes.idle_timeout()

    @staticmethod
    def session_tokens(user_id: int, user_session: UserSession) -> tuple[str, str, int]:
        """(access token, refresh token, access lifetime in seconds) of a live session. Neither outlives the hard
        end of the session (security.session_max_hours, when set); every refresh issues both again."""
        now = datetime.now(timezone.utc)
        hard_end = session_lifetimes.hard_end(user_session)
        access_expires = now + timedelta(minutes=settings.security.access_token_expire_minutes)
        refresh_expires = now + AuthService.refresh_window()
        if hard_end is not None:
            access_expires, refresh_expires = min(access_expires, hard_end), min(refresh_expires, hard_end)
        access = AuthService.create_access_token(user_id, session_id=user_session.id, not_after=access_expires)
        refresh = AuthService.create_refresh_token(user_id, session_id=user_session.id, expires_at=refresh_expires)
        return access, refresh, max(0, int((access_expires - now).total_seconds()))

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
            request_url: Optional[str] = None,
            certificate_fingerprint: Optional[str] = None
    ) -> UserSession:
        """Create a user session; with certificate_fingerprint (production login) it is bound to that certificate"""
        session_id = secrets.token_urlsafe(64)
        now = datetime.now(timezone.utc)

        user_session = UserSession(
            id=session_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=now,
            expires_at=now,
        )
        session_lifetimes.touch(user_session, now)
        await session_lifetimes.bind_certificate(user_session, certificate_fingerprint)
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
        cn_hash, _, fingerprint_hash = await hash_pki_certificate_data(
            certificate_cn, certificate_serial, certificate_fingerprint
        )
        stored_serial_forms = await serial_hashes(certificate_serial)

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
                    User.pki_certificate_serial_hash.in_(stored_serial_forms),
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
        """End a live session by logout. False when there is no such session or it has already ended."""
        query = select(UserSession).where(UserSession.id == session_id, UserSession.ended_at.is_(None))
        if user_id:
            query = query.where(UserSession.user_id == user_id)

        result = await session.exec(query)
        user_session = result.first()
        if user_session:
            state = session_lifetimes.session_state(user_session)
            if state.status is SessionStatus.ENDED:
                await session_lifetimes.record_end(session, user_session, state)
                return False

            session_lifetimes.end(user_session, SessionEndReason.LOGOUT)
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
            cause: SessionRevokeCause,
            actor_user_id: Optional[int] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            commit: bool = True
    ) -> int:
        """Revoke every live session of a user (deactivation, archive, new certificate, administrator).

        A session already past its absolute end or idle timeout is recorded with that reason, not as revoked.
        commit=False leaves the changes in the caller's transaction so that they land together with
        the change that caused them (the caller commits). Database errors propagate either way.
        Returns the number of sessions revoked.
        """
        result = await session.exec(
            select(UserSession).where(UserSession.user_id == user_id, UserSession.ended_at.is_(None))
        )
        not_ended = result.all()

        count = 0
        now = datetime.now(timezone.utc)
        for user_session in not_ended:
            state = session_lifetimes.session_state(user_session, now)
            if state.status is SessionStatus.ENDED:
                session_lifetimes.end(user_session, state.end_reason, at=state.ends_at)
            else:
                session_lifetimes.end(user_session, SessionEndReason.REVOKED, cause, at=now)
                count += 1
            session.add(user_session)

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
                    "message": f"All {count} live sessions revoked for user_id={user_id}",
                    "cause": cause.value
                },
                success=True
            )
            _logger.info(f"Revoked {count} live sessions for user_id={user_id}, cause={cause.value}")
        if commit and not_ended:
            await session.commit()

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
        cause = SessionRevokeCause.ACCOUNT_ARCHIVED if user.is_deleted else SessionRevokeCause.ACCOUNT_DEACTIVATED
        return await AuthService.invalidate_all_user_sessions(session, user_id=user.id, cause=cause)

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
