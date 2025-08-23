"""
Common dependencies for FastAPI routes.
Provides database sessions, authentication, and permission checking.
"""

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.security import decode_access_token, get_current_user
from backend.database.db_controls import get_session
from backend.database.models import User

# Database session dependency
DatabaseDep = Annotated[AsyncSession, Depends(get_session)]

# HTTP Bearer token security scheme
security = HTTPBearer(auto_error=False)
TokenDep = Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)]


async def get_current_active_user(
        db: DatabaseDep,
        token: TokenDep,
) -> User:
    """
    Get the current active user from the access token.
    Raises HTTPException if token is invalid or user is inactive.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(token.credentials)
        user = await get_current_user(db, payload)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Inactive user",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return user

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_current_superuser(
        current_user: Annotated[User, Depends(get_current_active_user)],
) -> User:
    """
    Get the current user and verify they have superuser privileges.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions"
        )
    return current_user


# Common dependency aliases
CurrentUser = Annotated[User, Depends(get_current_active_user)]
CurrentSuperuser = Annotated[User, Depends(get_current_superuser)]


def require_permissions(*required_permissions: str):
    """
    Dependency factory to check if user has required permissions.
    Returns a dependency function that validates permissions.

    Usage:
        @app.get("/protected")
        async def protected_route(
            user: Annotated[User, Depends(require_permissions("read:signals", "write:signals"))]
        ):
            ...
    """

    async def check_permissions(
            current_user: CurrentUser,
    ) -> User:
        # If no permissions required, just return user
        if not required_permissions:
            return current_user

        # Superusers have all permissions
        if current_user.is_superuser:
            return current_user

        # Check user permissions
        user_permissions = set()

        # Get permissions from user roles
        if current_user.roles:
            for role in current_user.roles:
                if role.permissions:
                    # Assuming permissions are stored as comma-separated string
                    role_perms = [p.strip() for p in role.permissions.split(",") if p.strip()]
                    user_permissions.update(role_perms)

        # Check if user has all required permissions
        missing_permissions = set(required_permissions) - user_permissions

        if missing_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permissions: {', '.join(missing_permissions)}"
            )

        return current_user

    return check_permissions


def require_roles(*required_roles: str):
    """
    Dependency factory to check if user has required roles.
    Returns a dependency function that validates roles.

    Usage:
        @app.get("/admin-only")
        async def admin_route(
            user: Annotated[User, Depends(require_roles("admin", "operator"))]
        ):
            ...
    """

    async def check_roles(
            current_user: CurrentUser,
    ) -> User:
        # If no roles required, just return user
        if not required_roles:
            return current_user

        # Superusers have all roles
        if current_user.is_superuser:
            return current_user

        # Get user role names
        user_roles = {role.name for role in current_user.roles} if current_user.roles else set()

        # Check if user has any of the required roles
        if not any(role in user_roles for role in required_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of the following roles: {', '.join(required_roles)}"
            )

        return current_user

    return check_roles


async def get_optional_user(
        db: DatabaseDep,
        token: TokenDep,
) -> Optional[User]:
    """
    Get the current user if authenticated, otherwise return None.
    Used for endpoints that work both authenticated and unauthenticated.
    """
    if not token:
        return None

    try:
        payload = decode_access_token(token.credentials)
        user = await get_current_user(db, payload)

        if user and user.is_active:
            return user

    except Exception:
        # Ignore authentication errors for optional authentication
        pass

    return None


# Optional user dependency
OptionalUser = Annotated[Optional[User], Depends(get_optional_user)]
