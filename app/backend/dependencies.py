from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from jose import JWTError, jwt
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from app.backend.config import settings
from app.backend.database import get_session
from app.backend.models.user import User

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials required"
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            settings.security.secret_key,
            algorithms=[settings.security.algorithm]
        )
        user_id: int = payload.get("sub")
        # Explicitly get sid or default to None
        session_id = payload.get("sid", None)
        
        # Store in request state for auditing
        request.state.session_id = session_id

        if user_id is None:
            logger.warning("Authentication failed: Missing user_id in JWT payload")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials"
            )
    except JWTError as e:
        logger.warning(f"JWT decode error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )

    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()

    if not user:
        logger.warning(f"User not found in database: user_id={user_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )

    if not user.is_active:
        logger.warning(f"Inactive user attempted to authenticate: user_id={user_id}, username={user.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )

    logger.info(f"User authenticated successfully: user_id={user_id}, username={user.username}")
    return user


async def get_current_active_user(
        current_user: User = Depends(get_current_user)
) -> User:
    """Ensure user is active"""
    logger.debug(f"Checking if user is active: user_id={current_user.id}, username={current_user.username}")

    if not current_user.is_active:
        logger.warning(f"Inactive user blocked: user_id={current_user.id}, username={current_user.username}")
        raise HTTPException(status_code=400, detail="Inactive user")

    logger.debug(f"Active user confirmed: user_id={current_user.id}, username={current_user.username}")
    return current_user


