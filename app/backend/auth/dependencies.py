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
from app.backend.auth.sessions import certificate_matches, live_session, touch
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

    # Account status first: a deactivated or archived account is rejected whatever the state of its sessions,
    # and any session still live is ended here so the token stops working even when the flag was changed
    # outside the API.
    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()

    if not user:
        logger.warning(f"User not found in database: user_id={user_id}")
        raise ApiError(AuthErrorCode.ACCOUNT_NOT_FOUND)

    if user.is_deleted or not user.is_active:
        ended = await AuthService.expire_sessions_of_inactive_user(session, user)
        if user.is_deleted:
            logger.warning(f"Deleted user attempted to authenticate: user_id={user_id}, username={user.username}, sessions ended={ended}")
            raise ApiError(AuthErrorCode.ACCOUNT_DELETED)
        logger.warning(f"Deactivated user attempted to authenticate: user_id={user_id}, username={user.username}, sessions ended={ended}")
        raise ApiError(AuthErrorCode.ACCOUNT_DEACTIVATED)

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
    if should_touch(user_session.last_activity):
        # Activity moves the end of the session (and feeds presence), at most once per touch interval
        touch(user_session)
        session.add(user_session)
        await session.commit()

    logger.debug(f"User authenticated: user_id={user_id}, username={user.username}")
    return user


async def get_auth_context(
        claims: TokenClaims = Depends(get_token_claims),
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
) -> AuthContext:
    """Authenticated user plus permissions, roles, post scope and the session of the token, loaded once per request"""
    return await load_auth_context(session, current_user, session_id=claims.session_id)
