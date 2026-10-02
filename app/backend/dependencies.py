from dataclasses import dataclass
from typing import Optional
from fastapi import Depends
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from app.backend.database import get_session
from app.backend.models.user import User, UserSession
from app.backend.enums.errors import AuthErrorCode
from app.backend.services.auth import AuthService
from app.backend.services.auth_context import AuthContext, load_auth_context
from app.backend.services.presence import should_touch, is_expired
from datetime import datetime, timezone
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
        session: AsyncSession = Depends(get_session)
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

    # Validate session is still active in DB (ensures invalidated sessions are rejected immediately)
    if session_id:
        session_result = await session.exec(
            select(UserSession).where(
                UserSession.id == session_id,
                UserSession.user_id == int(user_id),
                UserSession.is_active == True
            )
        )
        user_session = session_result.first()
        if user_session and is_expired(user_session.expires_at):
            # Enforce the session lifetime: mark it inactive and reject the request
            user_session.is_active = False
            user_session.ended_at = datetime.now(timezone.utc)
            session.add(user_session)
            await session.commit()
            logger.warning(f"Session expired: session_id={session_id}, user_id={user_id}")
            user_session = None
        if user_session and should_touch(user_session.last_activity):
            # Presence: refresh last_activity at most once per touch interval
            user_session.last_activity = datetime.now(timezone.utc)
            session.add(user_session)
            await session.commit()
        if not user_session:
            logger.warning(f"Session invalidated or not found: session_id={session_id}, user_id={user_id}")
            raise ApiError(AuthErrorCode.SESSION_EXPIRED)

    logger.info(f"User authenticated successfully: user_id={user_id}, username={user.username}")
    return user


async def get_auth_context(
        claims: TokenClaims = Depends(get_token_claims),
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
) -> AuthContext:
    """Authenticated user plus permissions, roles, post scope and the session of the token, loaded once per request"""
    return await load_auth_context(session, current_user, session_id=claims.session_id)
