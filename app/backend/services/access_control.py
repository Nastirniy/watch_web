from typing import Optional, Callable, Any
from fastapi import HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from loguru import logger

from app.backend.models.user import User
from app.backend.models.role import UserRole
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions


class AccessControlService:
    """
    Generic service for managing access control across different resources.
    Can be used for posts, observations, media, and any other resource that requires access checks.
    """

    @staticmethod
    async def check_post_membership(
        user: User,
        post_id: int,
        session: AsyncSession,
        operation: str = "access"
    ) -> None:
        """
        Check if user has access to the specified post.
        Users can access a post if:
        1. They have admin.true permission (bypass all checks)
        2. They are assigned to that specific post (user.post_id == post_id)

        Args:
            user: The user to check
            post_id: The post ID to check access for
            session: Database session
            operation: Description of the operation (for logging/error messages)

        Raises:
            HTTPException: If user doesn't have access to the post
        """
        # Check if user has admin.true permission (bypass post membership check)
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            logger.debug(f"Admin user {user.username} accessing post {post_id} for {operation}")
            return

        # Check if user is assigned to any post
        if user.post_id is None:
            logger.warning(f"User {user.username} tried to {operation} post {post_id} but is not assigned to any post")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not assigned to any post. Please contact an administrator."
            )

        # Check if user is assigned to the requested post
        if user.post_id != post_id:
            logger.warning(
                f"User {user.username} (post_id: {user.post_id}) tried to {operation} "
                f"post {post_id} without permission"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. You can only {operation} resources from your assigned post"
            )

        logger.debug(f"User {user.username} has access to post {post_id} for {operation}")

    @staticmethod
    async def check_resource_access(
        user: User,
        resource_post_id: int,
        session: AsyncSession,
        resource_type: str = "resource",
        operation: str = "access"
    ) -> None:
        """
        Generic method to check if user has access to a resource based on its post_id.
        Works for observations, media, or any other resource tied to a post.

        Args:
            user: The user to check
            resource_post_id: The post_id associated with the resource
            session: Database session
            resource_type: Type of resource (e.g., "observation", "media", "export")
            operation: Description of the operation (e.g., "view", "create", "update", "delete")

        Raises:
            HTTPException: If user doesn't have access

        Example:
            await AccessControlService.check_resource_access(
                user, observation.post_id, session, "observation", "update"
            )
        """
        await AccessControlService.check_post_membership(
            user,
            resource_post_id,
            session,
            f"{operation} {resource_type}"
        )

    @staticmethod
    async def check_ownership_or_admin(
        user: User,
        resource_owner_id: int,
        session: AsyncSession,
        resource_type: str = "resource"
    ) -> None:
        """
        Check if user is the owner of the resource OR has admin privileges.
        Useful for operations that should only be performed by the creator or admins.

        Args:
            user: The user to check
            resource_owner_id: The user_id who owns/created the resource
            session: Database session
            resource_type: Type of resource (for error messages)

        Raises:
            HTTPException: If user is not owner and not admin

        Example:
            await AccessControlService.check_ownership_or_admin(
                user, observation.user_id, session, "observation"
            )
        """
        # Check if user has admin.true permission
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            logger.debug(f"Admin user {user.username} accessing {resource_type} owned by user {resource_owner_id}")
            return

        # Check if user is the owner
        if user.id != resource_owner_id:
            logger.warning(
                f"User {user.username} (id: {user.id}) tried to access {resource_type} "
                f"owned by user {resource_owner_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You can only access your own {resource_type}"
            )

        logger.debug(f"User {user.username} is owner of {resource_type}")

    @staticmethod
    async def check_combined_access(
        user: User,
        resource_post_id: int,
        resource_owner_id: int,
        session: AsyncSession,
        resource_type: str = "resource",
        operation: str = "access",
        require_ownership: bool = False
    ) -> None:
        """
        Combined access check for resources that have both post_id and owner.
        Checks:
        1. Admin override (admin.true permission)
        2. Post membership
        3. Ownership (if required)

        Args:
            user: The user to check
            resource_post_id: The post_id associated with the resource
            resource_owner_id: The user_id who owns the resource
            session: Database session
            resource_type: Type of resource
            operation: Description of the operation
            require_ownership: If True, user must be owner (unless admin)

        Raises:
            HTTPException: If access is denied

        Example:
            # For viewing: only post membership required
            await AccessControlService.check_combined_access(
                user, observation.post_id, observation.user_id,
                session, "observation", "view", require_ownership=False
            )

            # For deletion: ownership required
            await AccessControlService.check_combined_access(
                user, observation.post_id, observation.user_id,
                session, "observation", "delete", require_ownership=True
            )
        """
        # Check admin override first
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            logger.debug(f"Admin user {user.username} {operation} {resource_type}")
            return

        # Check post membership
        await AccessControlService.check_post_membership(
            user, resource_post_id, session, f"{operation} {resource_type}"
        )

        # Check ownership if required
        if require_ownership and user.id != resource_owner_id:
            logger.warning(
                f"User {user.username} tried to {operation} {resource_type} "
                f"owned by user {resource_owner_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You can only {operation} your own {resource_type}"
            )

    @staticmethod
    def get_user_post_filter(user: User, bypass_for_admin: bool = True) -> Optional[int]:
        """
        Get the post_id filter for a user's queries.
        Returns None if user is admin (to show all posts).
        Returns user's post_id for regular users.

        Args:
            user: The user
            bypass_for_admin: If True, returns None for admins (no filter)

        Returns:
            post_id to filter by, or None for admins

        Example:
            post_filter = AccessControlService.get_user_post_filter(current_user)
            if post_filter:
                query = query.where(Observation.post_id == post_filter)
        """
        if bypass_for_admin:
            # Admins can see all posts - no filter needed
            # Note: This is a synchronous hint, async permission check should be done separately
            return None

        return user.post_id

    @staticmethod
    async def filter_query_by_post_access(
        user: User,
        session: AsyncSession,
        base_filter_value: Optional[int] = None
    ) -> Optional[int]:
        """
        Returns the appropriate post_id filter based on user's access level.
        - Admins: returns base_filter_value (could be None for all posts)
        - Regular users: returns their assigned post_id (ignores base_filter_value)

        Args:
            user: The user
            session: Database session
            base_filter_value: The requested filter (e.g., from query params)

        Returns:
            post_id to use in query filter

        Example:
            requested_post_id = request.query_params.get('post_id')
            actual_post_id = await AccessControlService.filter_query_by_post_access(
                current_user, session, requested_post_id
            )
            if actual_post_id:
                query = query.where(Observation.post_id == actual_post_id)
        """
        # Check if user has admin.true permission
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            # Admin can filter by any post or see all
            return base_filter_value

        # Regular user can only see their assigned post
        return user.post_id

    @staticmethod
    def is_post_member(user: User, post_id: int) -> bool:
        """
        Simple synchronous check if user is assigned to a post.
        Does not check admin permissions.

        Args:
            user: The user to check
            post_id: The post ID to check

        Returns:
            True if user.post_id matches post_id
        """
        return user.post_id == post_id

    @staticmethod
    async def check_role_membership(
        user: User,
        role_id: int,
        session: AsyncSession,
        operation: str = "view"
    ) -> None:
        """
        Check if user has the requested role or admin privileges.
        Users can access a role if:
        1. They have admin.true permission (bypass all checks)
        2. They are assigned to that specific role

        Args:
            user: The user to check
            role_id: The role ID to check access for
            session: Database session
            operation: Description of the operation (for logging/error messages)

        Raises:
            HTTPException: If user doesn't have the role

        Example:
            await AccessControlService.check_role_membership(
                current_user, role_id, session, "view"
            )
        """
        # Check if user has admin.true permission (bypass role membership check)
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            logger.debug(f"Admin user {user.username} accessing role {role_id} for {operation}")
            return

        # Check if user has the requested role
        result = await session.exec(
            select(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id == role_id
            )
        )
        user_role = result.first()

        if not user_role:
            logger.warning(
                f"User {user.username} (id: {user.id}) tried to {operation} "
                f"role {role_id} without having that role assigned"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. You can only {operation} roles that are assigned to you"
            )

        logger.debug(f"User {user.username} has access to role {role_id} for {operation}")

    @staticmethod
    async def check_user_self_or_admin(
        user: User,
        requested_user_id: int,
        session: AsyncSession,
        operation: str = "view"
    ) -> None:
        """
        Check if user is accessing their own data or has admin privileges.
        Users can access user data if:
        1. They have admin.true permission (bypass all checks)
        2. They are accessing their own user data (user.id == requested_user_id)

        Args:
            user: The current user making the request
            requested_user_id: The user ID being accessed
            session: Database session
            operation: Description of the operation (for logging/error messages)

        Raises:
            HTTPException: If user is not accessing themselves and not admin

        Example:
            await AccessControlService.check_user_self_or_admin(
                current_user, user_id, session, "view"
            )
        """
        # Check if user has admin.true permission (bypass self-access check)
        has_admin = await PermissionService.has_permission(
            session, user, Permissions.IS_ADMIN.value
        )

        if has_admin:
            logger.debug(f"Admin user {user.username} accessing user {requested_user_id} for {operation}")
            return

        # Check if user is accessing their own data
        if user.id != requested_user_id:
            logger.warning(
                f"User {user.username} (id: {user.id}) tried to {operation} "
                f"user {requested_user_id} without permission"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. You can only {operation} your own user information"
            )

        logger.debug(f"User {user.username} accessing their own data for {operation}")