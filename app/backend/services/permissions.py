from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from loguru import logger

from app.backend.models.user import User
from app.backend.models.role import UserRole, RolePermission, Permission
from app.backend.enums import Permissions


class PermissionService:
    @staticmethod
    async def get_user_permissions(session: AsyncSession, user: User) -> List[str]:
        """Get all permissions for a user"""
        # Get user roles
        user_roles_result = await session.exec(
            select(UserRole).where(UserRole.user_id == user.id)
        )
        user_roles = user_roles_result.all()

        permissions = set()

        # Get permissions for each role
        for user_role in user_roles:
            role_perms_result = await session.exec(
                select(RolePermission).where(RolePermission.role_id == user_role.role_id)
            )
            role_perms = role_perms_result.all()

            for role_perm in role_perms:
                perm_result = await session.exec(
                    select(Permission).where(Permission.id == role_perm.permission_id)
                )
                perm = perm_result.first()
                if perm:
                    permissions.add(perm.name)

        return list(permissions)

    @staticmethod
    async def has_permission(
            session: AsyncSession,
            user: User,
            permission: str
    ) -> bool:
        """Check if user has specific permission"""
        user_permissions = await PermissionService.get_user_permissions(session, user)

        # Check for admin override
        if Permissions.ADMIN_FULL.value in user_permissions:
            return True

        return permission in user_permissions

    @staticmethod
    def require_permission(permission: str):
        """Decorator to require permission for endpoint"""
        from functools import wraps
        from fastapi import Depends, HTTPException, status
        from app.backend.dependencies import get_current_user
        from app.backend.database import get_session

        def decorator(func):
            @wraps(func)
            async def wrapper(
                    *args,
                    current_user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session),
                    **kwargs
            ):
                has_perm = await PermissionService.has_permission(
                    session, current_user, permission
                )
                if not has_perm:
                    logger.warning(
                        f"Permission denied: user {current_user.username} lacks {permission}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Insufficient permissions"
                    )
                return await func(*args, current_user=current_user, session=session, **kwargs)

            return wrapper

        return decorator