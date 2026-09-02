from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from datetime import datetime, timezone

from app.backend.database import get_session
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.user import User
from app.backend.services.auth_context import AuthContext
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.services.permissions import require
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

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)
_logger = get_api_logger()


@router.get(SystemRoutes.ROOT.value, response_model=List[RoleResponse])
async def list_roles(
        filters: RoleFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """List all roles with filters"""
    current_user = auth.user
    try:
        AccessControlService.check_include_deleted(auth, filters, Permissions.ROLE_DELETE)
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
async def get_role_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """Available filter params and sortable fields for roles."""
    return EntitySchemaInfo(**RoleFilterService.get_schema(is_admin=auth.is_admin))


@router.post(SystemRoutes.ROOT.value, response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
        request: Request,
        role_data: RoleCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_CREATE], admin=True))
):
    """Create new role"""
    current_user = auth.user
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
async def list_all_permissions(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """List all available permissions in the system - admin only"""
    current_user = auth.user
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
async def get_role(
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """Get role by ID (administration area)"""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)

    return RoleResponse.model_validate(role)


@router.patch(RoleRoutes.ROLE_BY_ID.value, response_model=RoleResponse)
async def update_role(
        request: Request,
        role_id: int,
        role_update: RoleUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE], admin=True))
):
    """Update role and optionally replace all permissions"""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)

    # Store old values for auditing
    old_values = role.model_dump()

    # Update role fields
    if role_update.name is not None:
        await check_role_name_exists(session, role_update.name, exclude_id=role_id)
        role.name = role_update.name
    if role_update.display_name is not None:
        role.display_name = role_update.display_name
    if role_update.description is not None:
        role.description = role_update.description
    if role_update.is_deleted is not None:
        role.is_deleted = role_update.is_deleted

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
async def delete_role(
        request: Request,
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_DELETE], admin=True))
):
    """Soft-delete role. Deleted roles stop granting permissions, cannot be assigned and are hidden from lists unless include_deleted=true. Restore with PATCH is_deleted=false."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)

    # Store old values for auditing
    old_values = role.model_dump()

    # Soft delete: assignments stay for history, but the role stops applying immediately
    role.is_deleted = True
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    
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
async def list_role_permissions(
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """List all permissions assigned to a role"""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)

    result = await session.exec(
        select(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )

    permissions = result.all()
    _logger.info(f"User {current_user.username} listed {len(permissions)} permissions for role {role.name}")

    return [PermissionResponse.model_validate(perm) for perm in permissions]


async def _role_permissions(session: AsyncSession, role_id: int) -> List[PermissionResponse]:
    result = await session.exec(
        select(Permission).join(RolePermission, RolePermission.permission_id == Permission.id).where(RolePermission.role_id == role_id)
    )
    return [PermissionResponse.model_validate(perm) for perm in result.all()]


@router.put(RoleRoutes.ROLE_PERMISSION_BY_ID.value, response_model=List[PermissionResponse])
async def add_role_permission(
        request: Request,
        role_id: int,
        permission_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE], admin=True))
):
    """Grant one permission to a role (idempotent). Returns the role's permissions."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    permission = await get_permission_or_404(session, permission_id)

    existing = await session.exec(
        select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
    )
    if not existing.first():
        session.add(RolePermission(role_id=role_id, permission_id=permission_id))
        role.updated_at = datetime.now(timezone.utc)
        session.add(role)
        await session.commit()
        await create_audit_log(
            session=session,
            action="update",
            resource_type="role",
            resource_id=role.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            new_values={"permission_added": permission.name},
            details={"message": f"User {current_user.username} granted '{permission.name}' to role '{role.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()
        _logger.info(f"User {current_user.username} granted {permission.name} to role {role.name}")

    return await _role_permissions(session, role_id)


@router.delete(RoleRoutes.ROLE_PERMISSION_BY_ID.value, response_model=List[PermissionResponse])
async def remove_role_permission(
        request: Request,
        role_id: int,
        permission_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE], admin=True))
):
    """Revoke one permission from a role. Returns the role's remaining permissions."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    permission = await get_permission_or_404(session, permission_id)

    existing = await session.exec(
        select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
    )
    row = existing.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission is not assigned to this role")

    await session.delete(row)
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await session.commit()
    await create_audit_log(
        session=session,
        action="update",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values={"permission_removed": permission.name},
        details={"message": f"User {current_user.username} revoked '{permission.name}' from role '{role.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()
    _logger.info(f"User {current_user.username} revoked {permission.name} from role {role.name}")

    return await _role_permissions(session, role_id)
