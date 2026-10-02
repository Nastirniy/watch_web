from typing import Optional
from fastapi import Depends, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from jose import JWTError, jwt
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from app.backend.config import settings
from app.backend.database import get_session
from app.backend.models.user import User, UserSession
from app.backend.enums.errors import AuthErrorCode
from app.backend.services.auth import AuthService
from app.backend.services.auth_context import AuthContext, load_auth_context
from app.backend.services.presence import should_touch, is_expired
from datetime import datetime, timezone
from app.backend.services.api_errors import ApiError

security = HTTPBearer(auto_error=False)


async def get_current_user(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        session: AsyncSession = Depends(get_session)
) -> User:
    """Get current authenticated user"""
    logger.debug("Attempting to authenticate user")

    if not credentials:
        logger.warning("Authentication failed: No credentials provided")
        raise ApiError(AuthErrorCode.CREDENTIALS_REQUIRED)

    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            settings.security.secret_key,
            algorithms=[settings.security.algorithm]
        )
        if payload.get("type") != "access":
            logger.warning("Authentication failed: token is not an access token")
            raise ApiError(AuthErrorCode.INVALID_TOKEN)
        user_id: int = payload.get("sub")
        # Explicitly get sid or default to None
        session_id = payload.get("sid", None)

        # Store in request state for auditing
        request.state.session_id = session_id

        if user_id is None:
            logger.warning("Authentication failed: Missing user_id in JWT payload")
            raise ApiError(AuthErrorCode.INVALID_TOKEN)
    except JWTError as e:
        logger.warning(f"JWT decode error: {str(e)}")
        raise ApiError(AuthErrorCode.INVALID_TOKEN)

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
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session)
) -> AuthContext:
    """Authenticated user plus permissions, roles and post scope, loaded once per request"""
    return await load_auth_context(session, current_user)
