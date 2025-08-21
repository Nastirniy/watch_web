"""
User management API routes for CRUD operations and role assignments.
Handles user creation, updates, role management, and statistics.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import (
    AuthorizationException,
    ResourceConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from backend.core.permissions import SystemPermissions
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.user import user as user_crud, role as role_crud, permission as permission_crud
from backend.dependencies import DatabaseDep, CurrentUser, require_permissions
from backend.models.audit import AuditAction, AuditSeverity, ResourceType
from backend.schemas.base import (
    BulkOperationResponse,
    IdResponse,
    MessageResponse,
    PaginatedResponse,
    create_paginated_response,
    create_response,
)
from backend.schemas.user import (
    BulkRoleAssignment,
    Permission,
    PermissionCreate,
    PermissionUpdate,
    Role,
    RoleAssignment,
    RoleCreate,
    RoleStatistics,
    RoleUpdate,
    User,
    UserActivitySummary,
    UserCreate,
    UserSearchFilters,
    UserStatistics,
    UserSummary,
    UserUpdate,
)

router = APIRouter()


# User CRUD Operations
@router.post("", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(
        user_data: UserCreate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_CREATE)),
) -> User:
    """
    Create a new user.

    Args:
        user_data: User creation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created user

    Raises:
        ResourceConflictException: If username already exists
        ValidationException: If validation fails
    """
    try:
        # Check if username already exists
        existing_user = await user_crud.get_by_username(db, user_data.username)
        if existing_user:
            raise ResourceConflictException(
                detail=f"Username '{user_data.username}' already exists",
                field="username"
            )

        # Create user (without certificate info for now)
        user_dict = user_data.model_dump(exclude={"role_ids"})

        # Note: In real implementation, certificate info would be provided
        # For now, we'll generate placeholder values
        user_dict.update({
            "pki_certificate_cn": f"CN={user_data.username}",
            "pki_certificate_serial": f"SERIAL_{user_data.username}",
            "pki_certificate_fingerprint": f"FP_{user_data.username}",
        })

        new_user = await user_crud.create(db, user_dict)

        # Assign roles if specified
        if user_data.role_ids:
            for role_id in user_data.role_ids:
                await user_crud.assign_role(db, new_user.id, role_id, current_user.id)

        # Log user creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.CREATE,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_id=new_user.id,
            resource_name=new_user.username,
            new_values={"username": new_user.username, "roles": user_data.role_ids},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"User {new_user.username} created by {current_user.username}")

        # Return user with roles loaded
        return await user_crud.get_with_roles(db, new_user.id)

    except (ResourceConflictException, ValidationException):
        raise
    except Exception as e:
        logger.error(f"Error creating user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )


@router.get("", response_model=PaginatedResponse[UserSummary])
async def list_users(
        filters: UserSearchFilters = Depends(),
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_READ)),
) -> PaginatedResponse[UserSummary]:
    """
    List users with filtering and pagination.

    Args:
        filters: Search filters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of users
    """
    try:
        # Build filter dictionary
        filter_dict = {}
        if filters.is_active is not None:
            filter_dict["is_active"] = filters.is_active
        if filters.is_superuser is not None:
            filter_dict["is_superuser"] = filters.is_superuser
        if filters.is_verified is not None:
            filter_dict["is_verified"] = filters.is_verified
        if filters.post_id is not None:
            filter_dict["post_id"] = filters.post_id

        # Get users
        users = await user_crud.get_multi(
            db=db,
            skip=(filters.page - 1) * filters.per_page,
            limit=filters.per_page,
            filters=filter_dict,
            order_by=f"{'-' if filters.sort_order == 'desc' else ''}{filters.sort_by or 'created_at'}",
            load_relationships=["roles"],
        )

        # Get total count
        total = await user_crud.count(db, filter_dict)

        # Convert to summary format
        user_summaries = []
        for user in users:
            user_summaries.append(UserSummary(
                id=user.id,
                username=user.username,
                username_alias=user.username_alias,
                email=user.email,
                is_active=user.is_active,
                is_superuser=user.is_superuser,
                last_login_at=user.last_login_at,
                role_names=[role.name for role in user.roles] if user.roles else [],
                post_id=user.post_id,
            ))

        return create_paginated_response(
            items=user_summaries,
            total=total,
            page=filters.page,
            per_page=filters.per_page,
        )

    except Exception as e:
        logger.error(f"Error listing users: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users"
        )


@router.get("/{user_id}", response_model=User)
async def get_user(
        user_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_READ)),
) -> User:
    """
    Get user by ID.

    Args:
        user_id: User ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        User details

    Raises:
        ResourceNotFoundException: If user not found
    """
    user = await user_crud.get_with_roles(db, user_id)
    if not user:
        raise ResourceNotFoundException("User", user_id)

    return user


@router.put("/{user_id}", response_model=User)
async def update_user(
        user_id: int,
        user_data: UserUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_UPDATE)),
) -> User:
    """
    Update user.

    Args:
        user_id: User ID
        user_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated user

    Raises:
        ResourceNotFoundException: If user not found
    """
    # Get existing user
    existing_user = await user_crud.get(db, user_id)
    if not existing_user:
        raise ResourceNotFoundException("User", user_id)

    # Store old values for audit
    old_values = {
        "username_alias": existing_user.username_alias,
        "email": existing_user.email,
        "is_active": existing_user.is_active,
        "is_superuser": existing_user.is_superuser,
    }

    try:
        # Update user
        update_dict = user_data.model_dump(exclude_unset=True, exclude={"role_ids"})
        updated_user = await user_crud.update(db, existing_user, update_dict)

        # Update roles if specified
        if user_data.role_ids is not None:
            # Remove existing roles and assign new ones
            # Note: This is a simplified approach; in production, you might want
            # to be more selective about role changes
            current_roles = await user_crud.get_with_roles(db, user_id)
            if current_roles and current_roles.roles:
                for role in current_roles.roles:
                    await user_crud.remove_role(db, user_id, role.id)

            # Assign new roles
            for role_id in user_data.role_ids:
                await user_crud.assign_role(db, user_id, role_id, current_user.id)

        # Log user update
        new_values = update_dict.copy()
        if user_data.role_ids is not None:
            new_values["role_ids"] = user_data.role_ids

        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_id=user_id,
            resource_name=updated_user.username,
            old_values=old_values,
            new_values=new_values,
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"User {updated_user.username} updated by {current_user.username}")

        # Return user with roles loaded
        return await user_crud.get_with_roles(db, user_id)

    except Exception as e:
        logger.error(f"Error updating user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
        user_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_DELETE)),
) -> MessageResponse:
    """
    Delete user.

    Args:
        user_id: User ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Deletion confirmation

    Raises:
        ResourceNotFoundException: If user not found
        ValidationException: If trying to delete self
    """
    # Check if user exists
    user = await user_crud.get(db, user_id)
    if not user:
        raise ResourceNotFoundException("User", user_id)

    # Prevent self-deletion
    if user_id == current_user.id:
        raise ValidationException("Cannot delete your own account")

    try:
        # Soft delete user
        await user_crud.soft_delete(db, user_id)

        # Log user deletion
        await audit_crud.create_log(
            db=db,
            action=AuditAction.DELETE,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_id=user_id,
            resource_name=user.username,
            old_values={"username": user.username, "is_active": user.is_active},
            success=True,
            severity=AuditSeverity.HIGH,
        )

        logger.info(f"User {user.username} deleted by {current_user.username}")

        return MessageResponse(message="User deleted successfully")

    except Exception as e:
        logger.error(f"Error deleting user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user"
        )


# Role Management
@router.post("/{user_id}/roles", response_model=MessageResponse)
async def assign_role_to_user(
        user_id: int,
        role_assignment: RoleAssignment,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.ROLE_MANAGE)),
) -> MessageResponse:
    """
    Assign role to user.

    Args:
        user_id: User ID
        role_assignment: Role assignment data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Assignment confirmation
    """
    # Check if user exists
    user = await user_crud.get(db, user_id)
    if not user:
        raise ResourceNotFoundException("User", user_id)

    # Check if role exists
    role = await role_crud.get(db, role_assignment.role_id)
    if not role:
        raise ResourceNotFoundException("Role", role_assignment.role_id)

    try:
        # Assign role
        success = await user_crud.assign_role(
            db, user_id, role_assignment.role_id, current_user.id
        )

        if not success:
            return MessageResponse(message="Role already assigned to user")

        # Log role assignment
        await audit_crud.create_log(
            db=db,
            action=AuditAction.ROLE_ASSIGN,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_id=user_id,
            resource_name=user.username,
            new_values={"role_id": role_assignment.role_id, "role_name": role.name},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Role {role.name} assigned to user {user.username} by {current_user.username}")

        return MessageResponse(message="Role assigned successfully")

    except Exception as e:
        logger.error(f"Error assigning role to user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to assign role"
        )


@router.delete("/{user_id}/roles/{role_id}", response_model=MessageResponse)
async def remove_role_from_user(
        user_id: int,
        role_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.ROLE_MANAGE)),
) -> MessageResponse:
    """
    Remove role from user.

    Args:
        user_id: User ID
        role_id: Role ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Removal confirmation
    """
    # Check if user exists
    user = await user_crud.get(db, user_id)
    if not user:
        raise ResourceNotFoundException("User", user_id)

    # Check if role exists
    role = await role_crud.get(db, role_id)
    if not role:
        raise ResourceNotFoundException("Role", role_id)

    try:
        # Remove role
        success = await user_crud.remove_role(db, user_id, role_id)

        if not success:
            return MessageResponse(message="Role not assigned to user")

        # Log role removal
        await audit_crud.create_log(
            db=db,
            action=AuditAction.ROLE_REMOVE,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_id=user_id,
            resource_name=user.username,
            old_values={"role_id": role_id, "role_name": role.name},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Role {role.name} removed from user {user.username} by {current_user.username}")

        return MessageResponse(message="Role removed successfully")

    except Exception as e:
        logger.error(f"Error removing role from user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove role"
        )


# Bulk Operations
@router.post("/bulk/assign-roles", response_model=BulkOperationResponse)
async def bulk_assign_roles(
        bulk_assignment: BulkRoleAssignment,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.ROLE_MANAGE)),
) -> BulkOperationResponse:
    """
    Bulk assign roles to multiple users.

    Args:
        bulk_assignment: Bulk assignment data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Bulk operation result
    """
    processed = 0
    succeeded = 0
    errors = []

    try:
        for user_id in bulk_assignment.user_ids:
            for role_id in bulk_assignment.role_ids:
                processed += 1

                try:
                    # Check if user exists
                    user = await user_crud.get(db, user_id)
                    if not user:
                        errors.append({
                            "user_id": user_id,
                            "role_id": role_id,
                            "error": f"User {user_id} not found"
                        })
                        continue

                    # Assign role
                    success = await user_crud.assign_role(db, user_id, role_id, current_user.id)
                    if success:
                        succeeded += 1

                except Exception as e:
                    errors.append({
                        "user_id": user_id,
                        "role_id": role_id,
                        "error": str(e)
                    })

        # Log bulk operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.ROLE_ASSIGN,
            resource_type=ResourceType.USER,
            user_id=current_user.id,
            resource_name="bulk_operation",
            new_values={
                "user_ids": bulk_assignment.user_ids,
                "role_ids": bulk_assignment.role_ids,
                "processed": processed,
                "succeeded": succeeded,
            },
            success=len(errors) == 0,
            severity=AuditSeverity.MEDIUM,
        )

        return BulkOperationResponse(
            message=f"Bulk role assignment completed",
            processed=processed,
            succeeded=succeeded,
            failed=processed - succeeded,
            errors=errors,
        )

    except Exception as e:
        logger.error(f"Error in bulk role assignment: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bulk role assignment failed"
        )


# Statistics
@router.get("/statistics", response_model=UserStatistics)
async def get_user_statistics(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.USER_READ)),
) -> UserStatistics:
    """
    Get user statistics.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        User statistics
    """
    try:
        # Get various user counts
        total_users = await user_crud.count(db)
        active_users = await user_crud.count(db, {"is_active": True})
        verified_users = await user_crud.count(db, {"is_verified": True})
        superusers = await user_crud.count(db, {"is_superuser": True})
        users_with_posts = await user_crud.count(db, {"post_id": {"min": 1}})

        # Get recent logins (last 7 days)
        from datetime import datetime, timedelta
        week_ago = datetime.utcnow() - timedelta(days=7)
        recent_logins = await user_crud.count(db, {"last_login_at": {"min": week_ago}})

        # Get role distribution
        roles = await role_crud.get_multi(db, limit=100)
        by_role = {}
        for role in roles:
            # This would require a more complex query in real implementation
            by_role[role.name] = 0  # Placeholder

        return UserStatistics(
            total_users=total_users,
            active_users=active_users,
            verified_users=verified_users,
            superusers=superusers,
            users_with_posts=users_with_posts,
            recent_logins=recent_logins,
            by_role=by_role,
        )

    except Exception as e:
        logger.error(f"Error getting user statistics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user statistics"
        )
