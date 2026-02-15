from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from datetime import datetime, timezone

from app.backend.database import get_session
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.services.access_control import AccessControlService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import RoleRoutes, SystemRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.utils.validators import PaginationParams
from app.backend.utils.logging_config import get_logger, get_api_logger
from app.backend.filters.params.role_params import RoleFilterParams
from app.backend.filters.role_filter import RoleFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo
from app.backend.routes.helpers.roles_helper import (
    get_role_or_404,
    get_permission_or_404,
    check_role_name_exists
)
from app.backend.routes.models.request.roles_request import RoleCreate, RoleUpdate
from app.backend.routes.models.response.roles_response import RoleResponse, PermissionResponse

router = APIRouter()
_logger = get_api_logger()


@router.get(SystemRoutes.ROOT.value, response_model=List[RoleResponse])
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_roles(
        filters: RoleFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all roles with filters"""
    try:
        query = select(Role)
        query = RoleFilterService.apply_filters(query, filters)
        result = await session.exec(query)

        roles = result.all()
        _logger.info(f"User {current_user.username} listed {len(roles)} roles")

        return [RoleResponse.model_validate(role) for role in roles]

    except Exception as e:
        _logger.error(f"Error listing roles: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve roles"
        )


@router.get(RoleRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_role_filter_schema(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Available filter params and sortable fields for roles."""
    return EntitySchemaInfo(**RoleFilterService.get_schema(is_admin=True))


@router.post(SystemRoutes.ROOT.value, response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def create_role(
        request: Request,
        role_data: RoleCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Create new role"""
    # Preventive validation
    await check_role_name_exists(session, role_data.name)

    role = Role(**role_data.model_dump(exclude={"permission_ids"}))
    session.add(role)
    await session.flush()

    # Assign permissions if provided
    if role_data.permission_ids is not None:
        for perm_id in role_data.permission_ids:
            await get_permission_or_404(session, perm_id)
            session.add(RolePermission(role_id=role.id, permission_id=perm_id))

        _logger.info(
            f"User {current_user.username} created role {role.name} with {len(role_data.permission_ids)} permissions"
        )
    else:
        _logger.info(f"User {current_user.username} created role {role.name}")

    await session.commit()
    await session.refresh(role)

    # Create audit log
    await create_audit_log(
        session=session,
        action="create",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} created new role '{role.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()

    return RoleResponse.model_validate(role)


@router.get(RoleRoutes.ALL_PERMISSIONS.value, response_model=List[PermissionResponse])
@PermissionService.require_permissions({"all": [Permissions.IS_ADMIN.value]})
async def list_all_permissions(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all available permissions in the system - admin only"""
    try:
        result = await session.exec(
            select(Permission).order_by(Permission.name)
        )

        permissions = result.all()
        _logger.info(f"Admin {current_user.username} listed all {len(permissions)} permissions")

        return [PermissionResponse.model_validate(perm) for perm in permissions]

    except Exception as e:
        _logger.error(f"Error listing all permissions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve permissions"
        )


@router.get(RoleRoutes.ROLE_BY_ID.value, response_model=RoleResponse)
@PermissionService.require_permissions({"any": [
    Permissions.ROLE_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_role(
        role_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Get role by ID - users can only view roles assigned to them or if they have admin.true"""
    # Check if user has this role or is admin
    await AccessControlService.check_role_membership(
        current_user, role_id, session, "view"
    )

    role = await get_role_or_404(session, role_id)

    return RoleResponse.model_validate(role)


@router.patch(RoleRoutes.ROLE_BY_ID.value, response_model=RoleResponse)
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_UPDATE.value,
    Permissions.ROLE_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def update_role(
        request: Request,
        role_id: int,
        role_update: RoleUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Update role and optionally replace all permissions"""
    role = await get_role_or_404(session, role_id)

    # Store old values for auditing
    old_values = role.model_dump()

    # Update role fields
    if role_update.display_name is not None:
        role.display_name = role_update.display_name
    if role_update.description is not None:
        role.description = role_update.description

    role.updated_at = datetime.now(timezone.utc)
    session.add(role)

    # Replace all permissions if provided
    if role_update.permission_ids is not None:
        # Validate all permissions exist
        for perm_id in role_update.permission_ids:
            await get_permission_or_404(session, perm_id)

        # Delete all old permissions
        old_permissions = await session.exec(
            select(RolePermission).where(RolePermission.role_id == role_id)
        )
        for assignment in old_permissions.all():
            await session.delete(assignment)

        # Add all new permissions
        for perm_id in role_update.permission_ids:
            session.add(RolePermission(role_id=role_id, permission_id=perm_id))

        _logger.info(
            f"User {current_user.username} updated role {role.name} with {len(role_update.permission_ids)} permissions"
        )
    else:
        _logger.info(f"User {current_user.username} updated role {role.name}")

    await session.commit()
    await session.refresh(role)

    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} updated role '{role.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()

    return RoleResponse.model_validate(role)


@router.delete(RoleRoutes.ROLE_BY_ID.value, status_code=status.HTTP_204_NO_CONTENT)
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_DELETE.value,
    Permissions.IS_ADMIN.value]})
async def delete_role(
        request: Request,
        role_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Delete role"""
    role = await get_role_or_404(session, role_id)

    # Store old values for auditing
    old_values = role.model_dump()

    await session.delete(role)
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        details={"message": f"User {current_user.username} deleted role '{role.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()
    _logger.info(f"User {current_user.username} deleted role {role.name}")


# RolePermission Management Routes

@router.get(RoleRoutes.ROLE_PERMISSIONS.value, response_model=List[PermissionResponse])
@PermissionService.require_permissions({"all": [
    Permissions.ROLE_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_role_permissions(
        role_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all permissions assigned to a role"""
    role = await get_role_or_404(session, role_id)

    result = await session.exec(
        select(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )

    permissions = result.all()
    _logger.info(f"User {current_user.username} listed {len(permissions)} permissions for role {role.name}")

    return [PermissionResponse.model_validate(perm) for perm in permissions]
