from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import update
from sqlmodel import select, and_
import secrets

from app.backend.config import settings
from app.backend.utils.logging_config import get_auth_logger
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.enums.errors import AuthErrorCode
from app.backend.enums.general import CertificateState, SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.certificates import (
    validate_certificate_data,
    hash_certificate_data,
    serial_hashes,
    cn_hash_matches,
)
from app.backend.errors.coded import CodedError
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log, home_node, home_scope, user_facts
from app.backend.services.certificates import active_certificate, certificate_state
from app.backend.utils.row_locks import lock_all
from app.backend.utils.text import fit_column

# Get authentication logger
_logger = get_auth_logger()

_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12  # Higher security rounds
)


def truncate_for_logging(value: str, length: int = 16) -> str:
    """
    Truncate sensitive value for logging purposes

    Args:
        value: Value to truncate
        length: Number of characters to keep

    Returns:
        Truncated string with ellipsis
    """
    if not value:
        return ""
    return f"{value[:length]}..." if len(value) > length else value


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
    def create_refresh_token(user_id: int, session_id: Optional[str] = None, expires_at: Optional[datetime] = None,
                             jti: Optional[str] = None) -> str:
        """Create JWT refresh token (session_tokens sets expires_at and the jti the session stores; the session is
        checked at every refresh anyway)"""
        now = datetime.now(timezone.utc)
        expire = session_lifetimes.utc(expires_at) if expires_at is not None else now + AuthService.refresh_window()

        payload = {
            "sub": str(user_id),
            "exp": expire,
            "iat": now,
            "nbf": now,
            "type": "refresh",
            "jti": jti or session_lifetimes.new_refresh_jti()
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
        end of the session (security.session_max_hours, when set); every refresh issues both again. The refresh
        token carries the jti the session stores (create_session, sessions.claim_refresh): it is the only one that
        refreshes the session."""
        now = datetime.now(timezone.utc)
        hard_end = session_lifetimes.hard_end(user_session)
        access_expires = now + timedelta(minutes=settings.security.access_token_expire_minutes)
        refresh_expires = now + AuthService.refresh_window()
        if hard_end is not None:
            access_expires, refresh_expires = min(access_expires, hard_end), min(refresh_expires, hard_end)
        access = AuthService.create_access_token(user_id, session_id=user_session.id, not_after=access_expires)
        refresh = AuthService.create_refresh_token(user_id, session_id=user_session.id, expires_at=refresh_expires,
                                                   jti=user_session.refresh_jti)
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
        """Create a user session; with certificate_fingerprint (production login) it is bound to that certificate.

        A write of its own that begins with the user row locked shared (the account checks of authenticate_pki ran
        in a transaction committed before): a deactivation, an archive or a certificate revocation holds that row FOR
        UPDATE, so the login waits for it and checks the committed row again here. CodedError account_deleted /
        account_deactivated, and for a bound login certificate_revoked / certificate_not_recognized (as
        authenticate_pki answers) when the account or the certificate stopped being active meanwhile: no session
        of a deactivated account is ever stored. Committed here."""
        user_id = user.id
        locked = await session_lifetimes.begin_account_session_write(session, user_id)   # columns only, see there
        if locked is None:
            raise CodedError(AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED)
        if locked.is_deleted:
            _logger.warning(f"Login of a user archived during the login refused: user_id={user_id}")
            raise CodedError(AuthErrorCode.ACCOUNT_DELETED)
        if not locked.is_active:
            _logger.warning(f"Login of a user deactivated during the login refused: user_id={user_id}")
            raise CodedError(AuthErrorCode.ACCOUNT_DEACTIVATED)

        session_id = secrets.token_urlsafe(64)
        now = datetime.now(timezone.utc)

        user_session = UserSession(
            id=session_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=fit_column(UserSession.user_agent, user_agent),
            created_at=now,
            expires_at=now,
            refresh_jti=session_lifetimes.new_refresh_jti(),   # of the refresh token the login answers
        )
        session_lifetimes.touch(user_session, now)
        await session_lifetimes.bind_certificate(user_session, certificate_fingerprint)
        if user_session.certificate_fingerprint_hash:
            # A revocation holds the user row FOR UPDATE as well (services/certificates.py): read after the lock,
            # the certificate is as committed and stays so until the session is stored
            certificate = (await session.exec(select(UserCertificate).where(
                UserCertificate.user_id == user_id,
                UserCertificate.fingerprint_hash == user_session.certificate_fingerprint_hash,
            ).execution_options(populate_existing=True))).first()   # authenticate_pki's copy may still be held
            state = certificate_state(certificate) if certificate is not None else None
            if state is not CertificateState.ACTIVE:
                _logger.warning(f"Login with a certificate that stopped being active during the login refused: "
                                f"user_id={user_id}, state={state.value if state else 'missing'}")
                if state is CertificateState.REVOKED:
                    raise CodedError(AuthErrorCode.CERTIFICATE_REVOKED)
                raise CodedError(AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED)
        # No write to the users row: last_login_at is derived from the sessions (presence_properties.py), so two logins
        # of one user, or a login next to the user's other requests, never collide on it
        session.add(user_session)

        await create_audit_log(
            session,
            AuditEvent.USER_LOGIN,
            params={"username": locked.username},
            scope=home_node(locked.post_id),
            resource_id=user.id,
            user_id=user.id,
            session_id=session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            request_url=request_url,
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
        Authenticate user by PKI certificate with enhanced security: the three hashes of the certificate match a row
        of user_certificates (legacy rows exactly as the users columns did before 024). A revoked certificate answers
        certificate_revoked, an expired one certificate_not_recognized, then the account is checked.

        Returns:
            (user, None) on success
            (None, error_code) on failure -- error_code is an AuthErrorCode value
        """
        if not await validate_certificate_data(certificate_cn, certificate_serial, certificate_fingerprint):
            _logger.warning("Invalid PKI certificate data provided")
            return None, AuthErrorCode.CERTIFICATE_DATA_MISSING

        _logger.debug(
            f"PKI authentication attempt - CN: {certificate_cn}, "
            f"Serial: {truncate_for_logging(certificate_serial, 8)}, "
            f"Fingerprint: {truncate_for_logging(certificate_fingerprint)}"
        )

        # Hash the certificate data for secure comparison using helper
        _, _, fingerprint_hash = await hash_certificate_data(
            certificate_cn, certificate_serial, certificate_fingerprint
        )
        stored_serial_forms = await serial_hashes(certificate_serial)

        # Find the certificate and its user (without status filters first); the fingerprint hash is unique
        result = await session.exec(
            select(UserCertificate, User).join(User, User.id == UserCertificate.user_id).where(
                and_(
                    await cn_hash_matches(certificate_cn),
                    UserCertificate.serial_hash.in_(stored_serial_forms),
                    UserCertificate.fingerprint_hash == fingerprint_hash,
                )
            )
        )
        found = result.first()

        async def attempt(matched_user: Optional[User], certificate_id: Optional[int], success: bool) -> None:
            # The one row of the attempt, written from what the match found, just before the commit of the exit
            await create_audit_log(
                session,
                AuditEvent.AUTH_CERTIFICATE_LOGIN,
                params={
                    "certificate_cn": certificate_cn,
                    "certificate_serial": certificate_serial,
                    "matched": matched_user is not None,
                    "certificate_id": certificate_id,
                    "certificate_fingerprint_prefix": fingerprint_hash[:16],
                },
                scope=home_scope(matched_user) if matched_user is not None else None,
                success=success,
                user_id=matched_user.id if matched_user is not None else None,
                ip_address=ip_address,
            )

        if not found:
            _logger.warning(f"No user found for certificate CN: {certificate_cn}")
            await attempt(None, None, False)
            await session.commit()
            return None, AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED

        certificate, user = found
        state = certificate_state(certificate)
        if state is not CertificateState.ACTIVE:
            _logger.warning(f"Login with a {state.value} certificate: certificate_id={certificate.id}, user_id={user.id}")
            await attempt(user, certificate.id, False)
            await session.commit()
            if state is CertificateState.REVOKED:
                return None, AuthErrorCode.CERTIFICATE_REVOKED
            return None, AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED

        # Check account status separately for distinct error codes
        if user.is_deleted:
            _logger.warning(f"Deleted user attempted login: user_id={user.id}, username={user.username}")
            await attempt(user, certificate.id, False)
            await session.commit()
            return None, AuthErrorCode.ACCOUNT_DELETED

        if not user.is_active:
            _logger.warning(f"Deactivated user attempted login: user_id={user.id}, username={user.username}")
            await attempt(user, certificate.id, False)
            await session.commit()
            return None, AuthErrorCode.ACCOUNT_DEACTIVATED

        # Success
        await attempt(user, certificate.id, True)
        _logger.info(f"Successful PKI authentication for user {user.username}")

        await session.commit()
        return user, None

    @staticmethod
    async def invalidate_session(
            session: AsyncSession,
            session_id: str,
            user: User,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None
    ) -> bool:
        """End a live session of the user by logout. False when it is not the user's or has ended already (another
        request of it, an administrator, a revocation committed after this request was authenticated).

        A write of its own, like every request's write to its sessions: the shared user lock first
        (sessions.begin_session_write: it waits for an administrator ending the user's sessions), then one
        conditional UPDATE (ended_at still empty) with no read before it, so it never updates a session row changed
        after a read view (MariaDB error 1020), and the audit row only when it ended the session. Committed here."""
        user_id = user.id
        await session_lifetimes.begin_session_write(session, user_id)
        result = await session.exec(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.user_id == user_id, UserSession.ended_at.is_(None))
            .values(ended_at=datetime.now(timezone.utc), end_reason=SessionEndReason.LOGOUT.value, revoke_cause=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.commit()
            return False
        await create_audit_log(
            session,
            AuditEvent.SESSION_LOGOUT,
            params={"username": user.username},
            scope=home_scope(user),
            user_id=user_id,
            session_id=session_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await session.commit()
        return True

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
        """Revoke every live session of a user (deactivation, archive, new user type, administrator).

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
            username, home = await user_facts(session, user_id)
            await create_audit_log(
                session,
                AuditEvent.SESSION_END,
                params={"username": username, "cause": cause.value, "session_count": count},
                scope=home,
                resource_id=user_id,
                user_id=actor_user_id or user_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            _logger.info(f"Revoked {count} live sessions for user_id={user_id}, cause={cause.value}")
        if commit and not_ended:
            await session.commit()

        return count

    @staticmethod
    async def invalidate_certificate_sessions(
            session: AsyncSession,
            user_id: int,
            fingerprint_hash: str,
            cause: SessionRevokeCause,
            actor_user_id: Optional[int] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            commit: bool = True
    ) -> int:
        """Revoke the live sessions of a user bound to one certificate (certificate_fingerprint_hash); the user's
        other sessions stay. Same rules as invalidate_all_user_sessions: a session already past its end is
        recorded with that reason, commit=False leaves the changes in the caller's transaction.
        Returns the number of sessions revoked."""
        result = await session.exec(select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.certificate_fingerprint_hash == fingerprint_hash,
            UserSession.ended_at.is_(None),
        ))
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
            username, home = await user_facts(session, user_id)
            await create_audit_log(
                session,
                AuditEvent.SESSION_END,
                params={"username": username, "cause": cause.value, "session_count": count},
                scope=home,
                resource_id=user_id,
                user_id=actor_user_id or user_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            _logger.info(f"Revoked {count} live sessions of one certificate for user_id={user_id}, "
                         f"cause={cause.value}")
        if commit and not_ended:
            await session.commit()

        return count

    @staticmethod
    async def end_session_of_revoked_certificate(session: AsyncSession, user_session: UserSession, user: User,
                                                 begin: bool = True) -> bool:
        """End a live session bound to a certificate that is no active row of the user (revoked, expired, or not
        the user's): revoked, certificate_revoked.

        Revoking a certificate ends its live sessions in its own transaction; a login that read the row before the
        revocation and stored its session after it leaves a session bound to a revoked certificate. Called on every
        request and refresh of a bound session (one indexed lookup): True when it ended it (the caller answers
        token_certificate_mismatch). Unbound sessions (development logins) are never ended here.

        One conditional UPDATE (ended_at still empty), a write of its own beginning with the shared user lock
        (sessions.begin_session_write: the user row before the session row, as for an administrator ending the user's
        sessions, and the audit row's foreign key locks the user row again) unless begin=False (the refresh began it
        with that lock): parallel requests of the session end it once, the others find it ended and answer the
        same.
        """
        bound_to = user_session.certificate_fingerprint_hash
        if not bound_to or await active_certificate(session, user.id, bound_to) is not None:
            return False
        ended_at = datetime.now(timezone.utc)
        if begin:
            await session_lifetimes.begin_session_write(session, user.id)
        result = await session.exec(
            update(UserSession)
            .where(UserSession.id == user_session.id, UserSession.ended_at.is_(None))
            .values(ended_at=ended_at, end_reason=SessionEndReason.REVOKED.value,
                    revoke_cause=SessionRevokeCause.CERTIFICATE_REVOKED.value)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:          # another request of the session ended it first
            await session.commit()
            return True
        user_session.ended_at, user_session.end_reason = ended_at, SessionEndReason.REVOKED.value
        user_session.revoke_cause = SessionRevokeCause.CERTIFICATE_REVOKED.value
        await create_audit_log(
            session,
            AuditEvent.SESSION_END,
            params={"username": user.username, "cause": SessionRevokeCause.CERTIFICATE_REVOKED.value,
                    "ended_session_id": user_session.id},
            scope=home_scope(user),
            resource_id=user.id,
            user_id=user.id,
            session_id=user_session.id,
        )
        await session.commit()
        _logger.warning(f"Session bound to a certificate that is no longer active ended: user_id={user.id}")
        return True

    @staticmethod
    async def expire_sessions_of_inactive_user(session: AsyncSession, user_id: int) -> int:
        """End every live session of a user whose account is deactivated or archived.

        Called from the request path when such a user presents a token: the sessions were normally ended
        when the flag changed, but a flag flipped outside the API (direct database change, restore of a
        backup) would leave them live and count towards presence. Returns the number of sessions revoked.

        The flags are checked again on the locked, committed user row: an account reactivated since the caller read
        it ends nothing (0). The lock reads plain columns only (begin_account_session_write: no read view inside it)
        and leaves the caller's copy of the user as it was: the caller reads the user again after this commit
        (a new transaction) and answers as for an active user when the flags changed.

        A write of its own, like every request's write to the user's sessions: the shared user lock first
        (sessions.begin_account_session_write; the request is no administrator changing the account, which holds the
        row FOR UPDATE: parallel requests of the client share it and wait only for such a write), then one set-based
        conditional UPDATE of the live sessions with no plain read before it. Of parallel requests the first ends
        them and the others wait on the session rows, find them ended and change nothing: no session row changed
        after a read view (MariaDB error 1020), and one statement locks the rows in index order (no deadlock, 1213).
        Sessions past their end that nobody recorded are recorded with it after a locking read (no read view
        either). The caller's transaction must hold no change (start_write): the refresh rolls its claim back
        first. The audit row only when a session was revoked. Committed here."""
        account = await session_lifetimes.begin_account_session_write(session, user_id)
        if account is None or (account.is_active and not account.is_deleted):
            await session.commit()
            return 0
        cause = SessionRevokeCause.ACCOUNT_ARCHIVED if account.is_deleted else SessionRevokeCause.ACCOUNT_DEACTIVATED
        revoked = await session.exec(
            update(UserSession)
            .where(UserSession.user_id == user_id, session_lifetimes.live_clause())
            .values(ended_at=datetime.now(timezone.utc), end_reason=SessionEndReason.REVOKED.value,
                    revoke_cause=cause.value)
            .execution_options(synchronize_session=False)
        )
        count = revoked.rowcount
        past = await lock_all(session, select(UserSession).where(
            UserSession.user_id == user_id, UserSession.ended_at.is_(None)).order_by(UserSession.id))
        now = datetime.now(timezone.utc)
        for user_session in past:
            state = session_lifetimes.session_state(user_session, now)
            if state.status is SessionStatus.ENDED:
                session_lifetimes.end(user_session, state.end_reason, at=state.ends_at)
            else:                # live by the computation only after a clock step back: revoked as well
                session_lifetimes.end(user_session, SessionEndReason.REVOKED, cause, at=now)
                count += 1
            session.add(user_session)
        if count > 0:
            await create_audit_log(
                session,
                AuditEvent.SESSION_END,
                params={"username": account.username, "cause": cause.value, "session_count": count},
                scope=home_node(account.post_id),
                resource_id=user_id,
                user_id=user_id,
            )
            _logger.info(f"Revoked {count} live sessions for user_id={user_id}, cause={cause.value}")
        await session.commit()
        return count

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
