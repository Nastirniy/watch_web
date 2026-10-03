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
from app.backend.services.auth import AuthService
from app.backend.services.auth_context import AuthContext, load_auth_context
from app.backend.services.presence import should_touch
from app.backend.services.sessions import certificate_matches, live_session, touch
from app.backend.services.api_errors import ApiError

security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    session_id: Optional[str]


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
    return TokenClaims(user_id=user_id, session_id=payload.get("sid"))


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
    result = await session.exec(select(User).where(User.id == int(user_id)))
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
    # with a client certificate, this request must present the same certificate
    if session_id:
        user_session = await live_session(session, session_id, int(user_id))
        if user_session is None:
            raise ApiError(AuthErrorCode.SESSION_EXPIRED)
        if not await certificate_matches(user_session, x_ssl_client_fingerprint):
            logger.warning(f"Token presented with another client certificate: session_id={session_id}, user_id={user_id}")
            raise ApiError(AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH)
        if should_touch(user_session.last_activity):
            # Activity moves the end of the session (and feeds presence), at most once per touch interval
            touch(user_session)
            session.add(user_session)
            await session.commit()

    logger.info(f"User authenticated successfully: user_id={user_id}, username={user.username}")
    return user


async def get_auth_context(
        claims: TokenClaims = Depends(get_token_claims),
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
) -> AuthContext:
    """Authenticated user plus permissions, roles, post scope and the session of the token, loaded once per request"""
    return await load_auth_context(session, current_user, session_id=claims.session_id)
