from typing import List, Optional, Dict, Union, Any
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
                    Permission.name.in_([permission, Permissions.IS_ADMIN.value])
                )
            )
            
            result = await session.exec(query)
            user_permissions = result.all()

            # Check for admin override or specific permission
            has_perm = (
                Permissions.IS_ADMIN.value in user_permissions or
                permission in user_permissions
            )
            
            if Permissions.IS_ADMIN.value in user_permissions:
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

            # Check if user has admin.true which grants all permissions
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    Permission.name.in_(permissions + [Permissions.IS_ADMIN.value])
                )
            )

            result = await session.exec(query)
            user_permissions = result.all()

            # Check if user has admin.true or any of the required permissions
            if Permissions.IS_ADMIN.value in user_permissions:
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

            # Check if user has admin.true which grants all permissions
            query = (
                select(Permission.name)
                .select_from(Permission)
                .join(RolePermission, Permission.id == RolePermission.permission_id)
                .join(UserRole, RolePermission.role_id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    Permission.name.in_(permissions + [Permissions.IS_ADMIN.value])
                )
            )

            result = await session.exec(query)
            user_permissions = result.all()

            # Check if user has admin.true
            if Permissions.IS_ADMIN.value in user_permissions:
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
    async def evaluate_complex_permissions(
            session: AsyncSession,
            user: User,
            permission_structure: Union[str, List[str], Dict[str, Any]]
    ) -> bool:
        """
        Evaluate complex permission structures with nested AND/OR logic.

        Supported formats:
        - String: "permission.name" - single permission
        - List: ["perm1", "perm2"] - OR logic (any)
        - Dict with "any": {"any": ["perm1", "perm2"]} - OR logic
        - Dict with "all": {"all": ["perm1", "perm2"]} - AND logic
        - Nested: {"all": [{"any": ["perm1", "perm2"]}, "perm3"]} - complex logic

        Example:
            {"all": [{"any": ["posts.create", "posts.edit"]}, "user.verified"]}
            Means: (posts.create OR posts.edit) AND user.verified
        """
        try:
            if not user:
                logger.warning("Invalid user in evaluate_complex_permissions")
                return False

            if not user.is_active or user.is_deleted:
                logger.warning(f"Permission check denied for inactive user: {user.username}")
                return False

            # Handle string - single permission
            if isinstance(permission_structure, str):
                return await PermissionService.has_permission(session, user, permission_structure)

            # Handle list - treat as OR logic by default
            if isinstance(permission_structure, list):
                # Check if all items are strings
                if all(isinstance(item, str) for item in permission_structure):
                    return await PermissionService.has_any_permission(session, user, permission_structure)

                # Handle list with nested structures
                results = []
                for item in permission_structure:
                    result = await PermissionService.evaluate_complex_permissions(session, user, item)
                    results.append(result)
                # Default list behavior is OR
                return any(results)

            # Handle dictionary
            if isinstance(permission_structure, dict):
                # Check for "any" key (OR logic)
                if "any" in permission_structure:
                    any_perms = permission_structure["any"]
                    if not isinstance(any_perms, list):
                        logger.error(f"'any' key must contain a list, got {type(any_perms)}")
                        return False

                    results = []
                    for perm in any_perms:
                        result = await PermissionService.evaluate_complex_permissions(session, user, perm)
                        results.append(result)

                    any_result = any(results)

                    # Check for "all" key in the same dict (both conditions must be met)
                    if "all" in permission_structure:
                        all_perms = permission_structure["all"]
                        if not isinstance(all_perms, list):
                            logger.error(f"'all' key must contain a list, got {type(all_perms)}")
                            return False

                        all_results = []
                        for perm in all_perms:
                            result = await PermissionService.evaluate_complex_permissions(session, user, perm)
                            all_results.append(result)

                        all_result = all(all_results)
                        return any_result and all_result

                    return any_result

                # Check for "all" key (AND logic)
                elif "all" in permission_structure:
                    all_perms = permission_structure["all"]
                    if not isinstance(all_perms, list):
                        logger.error(f"'all' key must contain a list, got {type(all_perms)}")
                        return False

                    results = []
                    for perm in all_perms:
                        result = await PermissionService.evaluate_complex_permissions(session, user, perm)
                        results.append(result)

                    return all(results)

                else:
                    logger.error(f"Dict must contain 'any' or 'all' key, got: {permission_structure.keys()}")
                    return False

            logger.error(f"Unsupported permission structure type: {type(permission_structure)}")
            return False

        except Exception as e:
            logger.error(f"Error evaluating complex permissions for user {user.username if user else 'None'}: {e}")
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

    @staticmethod
    def require_permissions(permission_structure: Union[str, List[str], Dict[str, Any]]):
        """
        Decorator to require complex permission structures with nested AND/OR logic.

        Supported formats:
        - String: @require_permissions("permission.name")
        - List (OR): @require_permissions(["perm1", "perm2"])
        - Dict (AND): @require_permissions({"all": ["perm1", "perm2"]})
        - Dict (OR): @require_permissions({"any": ["perm1", "perm2"]})
        - Complex nested: @require_permissions({
            "all": [
                {"any": ["posts.create", "posts.edit"]},
                "user.verified"
            ]
          })

        Examples:
            # Single permission
            @require_permissions("posts.create")

            # User needs ANY of these (OR logic)
            @require_permissions(["posts.create", "posts.edit"])
            @require_permissions({"any": ["posts.create", "posts.edit"]})

            # User needs ALL of these (AND logic)
            @require_permissions({"all": ["posts.create", "user.verified"]})

            # Complex: (posts.create OR posts.edit) AND user.verified
            @require_permissions({
                "all": [
                    {"any": ["posts.create", "posts.edit"]},
                    "user.verified"
                ]
            })

            # Complex: posts.create AND (comments.edit OR comments.delete)
            @require_permissions({
                "all": [
                    "posts.create",
                    {"any": ["comments.edit", "comments.delete"]}
                ]
            })
        """
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
                has_perm = await PermissionService.evaluate_complex_permissions(
                    session, current_user, permission_structure
                )
                if not has_perm:
                    logger.warning(
                        f"Permission denied: user {current_user.username} failed complex permission check: {permission_structure}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Insufficient permissions"
                    )
                return await func(*args, current_user=current_user, session=session, **kwargs)

            return wrapper

        return decorator
