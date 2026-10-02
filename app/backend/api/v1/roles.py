from typing import List
from fastapi import APIRouter, Depends, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy import delete as sa_delete
from datetime import datetime, timezone

from app.backend.database import get_session, ADMIN_ROLE_NAME
from app.backend.models.role import Role, Permission, RolePermission, UserRole
from app.backend.models.user import User
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.services.access_control import AccessControlService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import RoleRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.filters.params.role_params import RoleFilterParams
from app.backend.filters.role_filter import RoleFilterService
from app.backend.api.shared.models.response.search_response import EntitySchemaInfo
from app.backend.api.shared.helpers.roles_helper import (
    get_role_or_404,
    get_permission_or_404,
    check_role_name_exists
)
from app.backend.api.shared.models.request.roles_request import RoleCreate, RoleUpdate
from app.backend.api.shared.models.response.roles_response import RoleResponse, PermissionResponse
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.openapi_problems import ADMIN, problems

router = APIRouter(prefix=RoleRoutes.BASE.value, tags=[RoleRoutes.tag()])
_logger = get_api_logger()


@router.get(
    RoleRoutes.ROOT.value,
    response_model=List[RoleResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, ErrorCode.ROLES_LIST_FAILED, *ADMIN),
)
async def list_roles(
        filters: RoleFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """List roles with filters. include_deleted=true (archived roles too) requires role.archive."""
    current_user = auth.user
    try:
        AccessControlService.check_include_deleted(auth, filters, Permissions.ROLE_ARCHIVE)
        query = select(Role)
        query = RoleFilterService.apply_filters(query, filters)
        result = await session.exec(query)

        roles = result.all()
        _logger.info(f"User {current_user.username} listed {len(roles)} roles")

        return [RoleResponse.model_validate(role) for role in roles]

    except Exception as e:
        _logger.error(f"Error listing roles: {str(e)}")
        raise ApiError(ErrorCode.ROLES_LIST_FAILED)


@router.get(RoleRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo, responses=problems(*ADMIN))
async def get_role_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """Available filter params and sortable fields for roles."""
    return EntitySchemaInfo(**RoleFilterService.get_schema(is_admin=auth.is_admin))


@router.post(
    RoleRoutes.ROOT.value,
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.PERMISSION_ID_INVALID, ErrorCode.ROLE_NAME_TAKEN, ErrorCode.PERMISSION_NOT_FOUND,
        ErrorCode.ROLE_NAME_ARCHIVED, *ADMIN,
    ),
)
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
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} created new role '{role.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return RoleResponse.model_validate(role)


@router.get(
    RoleRoutes.ALL_PERMISSIONS.value,
    response_model=List[PermissionResponse],
    responses=problems(ErrorCode.PERMISSIONS_LIST_FAILED, *ADMIN),
)
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
        raise ApiError(ErrorCode.PERMISSIONS_LIST_FAILED)


@router.get(
    RoleRoutes.ROLE_BY_ID.value,
    response_model=RoleResponse,
    responses=problems(ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NOT_FOUND, *ADMIN),
)
async def get_role(
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True))
):
    """Get role by ID (administration area)"""
    role = await get_role_or_404(session, role_id, include_deleted=True)

    return RoleResponse.model_validate(role)


@router.patch(
    RoleRoutes.ROLE_BY_ID.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.PERMISSION_ID_INVALID, ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NAME_TAKEN,
        ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_NAME_ARCHIVED, *ADMIN,
    ),
)
async def update_role(
        request: Request,
        role_id: int,
        role_update: RoleUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE], admin=True))
):
    """Update role and optionally replace all permissions. Archived roles answer 404 until restored (POST /{role_id}/restore)."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)

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
        old_values=old_values,
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} updated role '{role.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return RoleResponse.model_validate(role)


@router.post(
    RoleRoutes.ROLE_ARCHIVE.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ADMIN_ROLE_NOT_ARCHIVABLE, *ADMIN,
    ),
)
async def archive_role(
        request: Request,
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_ARCHIVE], admin=True))
):
    """Archive a role (is_deleted=true). Nothing is removed: user assignments and permissions stay in the database.

    An archived role stops granting permissions immediately, cannot be assigned and is hidden from lists
    unless include_deleted=true. Undo with POST /{role_id}/restore. Already archived or unknown: 404.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    if role.name == ADMIN_ROLE_NAME:
        raise ApiError(ErrorCode.ADMIN_ROLE_NOT_ARCHIVABLE)

    old_values = role.model_dump()
    role.is_deleted = True
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await create_audit_log(
        session=session,
        action="archive",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} archived role '{role.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(role)
    _logger.info(f"User {current_user.username} archived role {role.name}")
    return RoleResponse.model_validate(role)


@router.post(
    RoleRoutes.ROLE_RESTORE.value,
    response_model=RoleResponse,
    responses=problems(ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_NOT_ARCHIVED, *ADMIN),
)
async def restore_role(
        request: Request,
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_ARCHIVE], admin=True))
):
    """Restore an archived role (is_deleted=false): it grants its permissions again and can be assigned. Not archived: 409."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_NOT_ARCHIVED)

    old_values = role.model_dump()
    role.is_deleted = False
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await create_audit_log(
        session=session,
        action="restore",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=role.model_dump(),
        details={"message": f"User {current_user.username} restored role '{role.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(role)
    _logger.info(f"User {current_user.username} restored role {role.name}")
    return RoleResponse.model_validate(role)


@router.delete(
    RoleRoutes.ROLE_BY_ID.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ADMIN_ROLE_NOT_DELETABLE,
        ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE, ErrorCode.ROLE_HAS_ACTIVE_USERS, *ADMIN,
    ),
)
async def delete_role(
        request: Request,
        role_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_DELETE], admin=True))
):
    """PERMANENTLY delete a role. This is the only hard delete in the API and cannot be undone.

    Requires role.delete (distinct from role.archive). The role row, its permission grants and its
    assignments to archived users are removed from the database. Preconditions, each answered with 409:
    the role must be archived first (POST /{role_id}/archive), no active user may still hold it
    (the response lists them), and the built-in admin role can never be deleted.
    The audit log keeps a full copy of the role, its permissions and its assignments.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)
    if role.name == ADMIN_ROLE_NAME:
        raise ApiError(ErrorCode.ADMIN_ROLE_NOT_DELETABLE)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE)

    assigned = await session.exec(
        select(User.id, User.username, User.is_deleted)
        .join(UserRole, UserRole.user_id == User.id)
        .where(UserRole.role_id == role_id)
        .order_by(User.id)
    )
    assigned_rows = assigned.all()
    active_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if not deleted]
    if active_users:
        raise ApiError(ErrorCode.ROLE_HAS_ACTIVE_USERS, assigned_users=active_users)
    archived_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if deleted]

    permissions = await session.exec(
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
        .order_by(Permission.name)
    )
    permission_names = list(permissions.all())
    old_values = {**role.model_dump(), "permissions": permission_names, "assigned_archived_users": archived_users}

    # Core statements on purpose: no ORM cascade (no lazy loads in an async session), every row is removed explicitly
    await session.execute(sa_delete(RolePermission).where(RolePermission.role_id == role_id))
    await session.execute(sa_delete(UserRole).where(UserRole.role_id == role_id))
    await session.execute(sa_delete(Role).where(Role.id == role_id))
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="role",
        resource_id=role_id,
        user_id=current_user.id,
        old_values=old_values,
        details={
            "message": (
                f"User {current_user.username} permanently deleted role '{old_values['name']}' "
                f"({len(permission_names)} permissions, {len(archived_users)} assignments to archived users)"
            ),
            "permanent": True,
        },
        **client_info(request, auth),
    )
    await session.commit()
    _logger.warning(f"User {current_user.username} permanently deleted role {old_values['name']} (id {role_id})")


# RolePermission Management Routes

@router.get(
    RoleRoutes.ROLE_PERMISSIONS.value,
    response_model=List[PermissionResponse],
    responses=problems(ErrorCode.ROLE_ID_INVALID, ErrorCode.ROLE_NOT_FOUND, *ADMIN),
)
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


@router.put(
    RoleRoutes.ROLE_PERMISSION_BY_ID.value,
    response_model=List[PermissionResponse],
    responses=problems(
        ErrorCode.PERMISSION_ID_INVALID, ErrorCode.ROLE_ID_INVALID, ErrorCode.PERMISSION_NOT_FOUND,
        ErrorCode.ROLE_NOT_FOUND, *ADMIN,
    ),
)
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
            new_values={"permission_added": permission.name},
            details={"message": f"User {current_user.username} granted '{permission.name}' to role '{role.name}'"},
            **client_info(request, auth)
        )
        await session.commit()
        _logger.info(f"User {current_user.username} granted {permission.name} to role {role.name}")

    return await _role_permissions(session, role_id)


@router.delete(
    RoleRoutes.ROLE_PERMISSION_BY_ID.value,
    response_model=List[PermissionResponse],
    responses=problems(
        ErrorCode.PERMISSION_ID_INVALID, ErrorCode.ROLE_ID_INVALID, ErrorCode.PERMISSION_NOT_FOUND,
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_PERMISSION_NOT_ASSIGNED, *ADMIN,
    ),
)
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
        raise ApiError(ErrorCode.ROLE_PERMISSION_NOT_ASSIGNED)

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
        old_values={"permission_removed": permission.name},
        details={"message": f"User {current_user.username} revoked '{permission.name}' from role '{role.name}'"},
        **client_info(request, auth)
    )
    await session.commit()
    _logger.info(f"User {current_user.username} revoked {permission.name} from role {role.name}")

    return await _role_permissions(session, role_id)
