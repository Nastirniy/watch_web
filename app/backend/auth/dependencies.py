from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, Header
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from app.backend.database import get_session
from app.backend.models.user import User
from app.backend.enums.errors import AuthErrorCode
from app.backend.auth.service import AuthService
from app.backend.auth.context import AuthContext, load_auth_context
from app.backend.auth.presence import should_touch
from app.backend.auth.sessions import TouchResult, certificate_matches, live_session, touch_session
from app.backend.errors.problems import ApiError

security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    session_id: str


async def get_token_claims(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> TokenClaims:
    """The verified access token of the request (FastAPI caches it per request)"""
    if not credentials:
        logger.warning("Authentication failed: No credentials provided")
        raise ApiError(AuthErrorCode.CREDENTIALS_REQUIRED)
    payload = AuthService.verify_token(credentials.credentials, token_type="access")
    if payload is None:
        raise ApiError(AuthErrorCode.INVALID_TOKEN)
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Authentication failed: no valid user id in the token")
        raise ApiError(AuthErrorCode.INVALID_TOKEN)
    # The login binds every token to its session (sid); a token without one would skip the session checks
    session_id = payload.get("sid")
    if not session_id or not isinstance(session_id, str):
        logger.warning(f"Authentication failed: the token names no session, user_id={user_id}")
        raise ApiError(AuthErrorCode.INVALID_TOKEN)
    return TokenClaims(user_id=user_id, session_id=session_id)


async def get_current_user(
        claims: TokenClaims = Depends(get_token_claims),
        session: AsyncSession = Depends(get_session),
        # Set by nginx from the verified client certificate (nginx-proxy.conf), never by the client
        x_ssl_client_fingerprint: Optional[str] = Header(None, include_in_schema=False),
) -> User:
    """Get current authenticated user"""
    user_id = claims.user_id
    session_id = claims.session_id

    # Account status first (_refuse_inactive_account)
    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()

    if not user:
        logger.warning(f"User not found in database: user_id={user_id}")
        raise ApiError(AuthErrorCode.ACCOUNT_NOT_FOUND)
    await _refuse_inactive_account(session, user)

    # The session of the token must be live (not ended, used within the idle timeout) and, when it was opened
    # with a client certificate, bound to an active certificate of the user and presented with that certificate
    user_session = await live_session(session, session_id, user_id)
    if user_session is None:
        raise ApiError(AuthErrorCode.SESSION_EXPIRED)
    if await AuthService.end_session_of_revoked_certificate(session, user_session, user):
        raise ApiError(AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH)
    if not await certificate_matches(user_session, x_ssl_client_fingerprint):
        logger.warning(f"Token presented with another client certificate: session_id={session_id}, user_id={user_id}")
        raise ApiError(AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH)
    if should_touch(user_session.last_activity) and \
            await touch_session(session, user_session) is TouchResult.ENDED:
        # Activity moves the end of the session (and feeds presence), at most once per touch interval; a write of
        # its own, so parallel requests of the session never answer 1020 (auth/sessions.py touch_session). The touch
        # waited for an administrator who ended the session (an end of the user's sessions, a deactivation, an
        # archive, a certificate revocation): the request answers what the next one of the session would, the
        # account first (account_deactivated / account_deleted), else session_expired whatever ended it
        await _refuse_inactive_account(session, await _committed_user(session, user_id))
        logger.warning(f"Session ended while its touch waited: session_id={session_id}, user_id={user_id}, "
                       f"revoke_cause={user_session.revoke_cause}")
        raise ApiError(AuthErrorCode.SESSION_EXPIRED)

    logger.debug(f"User authenticated: user_id={user_id}, username={user.username}")
    return user


async def _committed_user(session: AsyncSession, user_id: int) -> User:
    """The user row as committed now (a statement after the touch's commit: a new read view on MariaDB)"""
    user = (await session.exec(
        select(User).where(User.id == user_id).execution_options(populate_existing=True))).first()
    if user is None:
        raise ApiError(AuthErrorCode.ACCOUNT_NOT_FOUND)
    return user


async def _refuse_inactive_account(session: AsyncSession, user: User) -> None:
    """A deactivated or archived account is refused whatever the state of its sessions, and any session of it still
    live is ended here, so the token stops working even when the flag was changed outside the API. The flags are
    checked again on the locked row (expire_sessions_of_inactive_user puts it into `user`): an account reactivated
    since the request read it is let through, the request goes on as for an active user."""
    if not user.is_deleted and user.is_active:
        return
    user_id, username = user.id, user.username
    ended = await AuthService.expire_sessions_of_inactive_user(session, user_id)
    if not user.is_deleted and user.is_active:
        logger.info(f"Account reactivated before its sessions were ended: user_id={user_id}")
        return
    if user.is_deleted:
        logger.warning(f"Deleted user attempted to authenticate: user_id={user_id}, username={username}, sessions ended={ended}")
        raise ApiError(AuthErrorCode.ACCOUNT_DELETED)
    logger.warning(f"Deactivated user attempted to authenticate: user_id={user_id}, username={username}, sessions ended={ended}")
    raise ApiError(AuthErrorCode.ACCOUNT_DEACTIVATED)


async def get_auth_context(
        claims: TokenClaims = Depends(get_token_claims),
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
) -> AuthContext:
    """Authenticated user plus permissions, roles, post scope and the session of the token, loaded once per request"""
    return await load_auth_context(session, current_user, session_id=claims.session_id)
