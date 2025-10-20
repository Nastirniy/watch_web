from typing import List, Optional
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from loguru import logger

from app.backend.models.user import User
from app.backend.models.role import UserRole, RolePermission, Permission
from app.backend.enums.general import Permissions


class PermissionService:
    @staticmethod
    async def get_user_permissions(session: AsyncSession, user: User) -> List[str]:
        """Get all permissions for a user with optimized SQLAlchemy query"""
        try:
            # Check if user is active and not deleted
            if not user.is_active or user.is_deleted:
                logger.warning(f"Permission check for inactive/deleted user: {user.username}")
                return []

            # Use JOIN query for better performance
            from sqlalchemy.orm import selectinload
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(UserRole.user_id == user.id)
                .distinct()
            )
            
            result = await session.exec(query)
            permissions = result.all()
            
            logger.debug(f"User {user.username} has permissions: {permissions}")
            return list(permissions)
            
        except Exception as e:
            logger.error(f"Error getting permissions for user {user.username}: {e}")
            return []

    @staticmethod
    async def has_permission(
            session: AsyncSession,
            user: User,
            permission: str
    ) -> bool:
        """Check if user has specific permission with enhanced validation"""
        try:
            # Basic validation
            if not user or not permission:
                logger.warning("Invalid user or permission in has_permission check")
                return False
                
            # Check if user is active
            if not user.is_active or user.is_deleted:
                logger.warning(f"Permission check denied for inactive user: {user.username}")
                return False

            # Direct query to check specific permission (more efficient)
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    Permission.name.in_([permission, Permissions.ADMIN_FULL.value])
                )
            )
            
            result = await session.exec(query)
            user_permissions = result.all()

            # Check for admin override or specific permission
            has_perm = (
                Permissions.ADMIN_FULL.value in user_permissions or
                permission in user_permissions
            )
            
            if Permissions.ADMIN_FULL.value in user_permissions:
                logger.debug(f"Admin override granted for user {user.username}")
            elif not has_perm:
                logger.debug(f"Permission {permission} denied for user {user.username}")
            
            return has_perm
            
        except Exception as e:
            logger.error(f"Error checking permission {permission} for user {user.username if user else 'None'}: {e}")
            return False

    @staticmethod
    async def has_any_permission(
            session: AsyncSession,
            user: User,
            permissions: List[str]
    ) -> bool:
        """Check if user has ANY of the specified permissions (OR logic)"""
        try:
            if not user or not permissions:
                logger.warning("Invalid user or permissions in has_any_permission check")
                return False

            if not user.is_active or user.is_deleted:
                logger.warning(f"Permission check denied for inactive user: {user.username}")
                return False

            # Check if user has admin.full which grants all permissions
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    Permission.name.in_(permissions + [Permissions.ADMIN_FULL.value])
                )
            )

            result = await session.exec(query)
            user_permissions = result.all()

            # Check if user has admin.full or any of the required permissions
            if Permissions.ADMIN_FULL.value in user_permissions:
                logger.debug(f"Admin override granted for user {user.username}")
                return True

            has_any = any(perm in user_permissions for perm in permissions)

            if not has_any:
                logger.debug(f"User {user.username} lacks any of permissions: {permissions}")

            return has_any

        except Exception as e:
            logger.error(f"Error checking any permission for user {user.username if user else 'None'}: {e}")
            return False

    @staticmethod
    async def has_all_permissions(
            session: AsyncSession,
            user: User,
            permissions: List[str]
    ) -> bool:
        """Check if user has ALL of the specified permissions (AND logic)"""
        try:
            if not user or not permissions:
                logger.warning("Invalid user or permissions in has_all_permissions check")
                return False

            if not user.is_active or user.is_deleted:
                logger.warning(f"Permission check denied for inactive user: {user.username}")
                return False

            # Check if user has admin.full which grants all permissions
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    Permission.name.in_(permissions + [Permissions.ADMIN_FULL.value])
                )
            )

            result = await session.exec(query)
            user_permissions = result.all()

            # Check if user has admin.full
            if Permissions.ADMIN_FULL.value in user_permissions:
                logger.debug(f"Admin override granted for user {user.username}")
                return True

            has_all = all(perm in user_permissions for perm in permissions)

            if not has_all:
                missing = [p for p in permissions if p not in user_permissions]
                logger.debug(f"User {user.username} missing permissions: {missing}")

            return has_all

        except Exception as e:
            logger.error(f"Error checking all permissions for user {user.username if user else 'None'}: {e}")
            return False

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

    @staticmethod
    def require_any_permission(*permissions: str):
        """Decorator to require ANY of the specified permissions (OR logic)"""
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
                has_perm = await PermissionService.has_any_permission(
                    session, current_user, list(permissions)
                )
                if not has_perm:
                    logger.warning(
                        f"Permission denied: user {current_user.username} lacks any of {permissions}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Insufficient permissions"
                    )
                return await func(*args, current_user=current_user, session=session, **kwargs)

            return wrapper

        return decorator

    @staticmethod
    def require_all_permissions(*permissions: str):
        """Decorator to require ALL of the specified permissions (AND logic)"""
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
                has_perm = await PermissionService.has_all_permissions(
                    session, current_user, list(permissions)
                )
                if not has_perm:
                    logger.warning(
                        f"Permission denied: user {current_user.username} lacks all of {permissions}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Insufficient permissions"
                    )
                return await func(*args, current_user=current_user, session=session, **kwargs)

            return wrapper

        return decorator