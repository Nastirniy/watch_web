from typing import List, Annotated
from fastapi import APIRouter, Depends, status, Request, Query, Path
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone

from app.backend.api.shared.helpers.db_errors import is_unique_violation, unique_violation_as
from app.backend.database import get_session
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.user import User
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.assignments import check_role_permissions_added
from app.backend.auth.privileges import check_grant, role_permission_names
from app.backend.enums.general import Permissions
from app.backend.enums.routes import RoleRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.api.v1.models.query.roles import RoleFilterParams
from app.backend.filters.role_filter import RoleFilterService
from app.backend.api.shared.helpers.roles_helper import (
    get_role_or_404,
    permission_names_by_role,
    get_permission_or_404,
    check_role_name_exists
)
from app.backend.api.v1.models.request.roles import RoleClone, RoleCreate, RoleUpdate
from app.backend.api.v1.models.response.roles import (
    RoleResponse, PermissionResponse, role_to_response, permission_to_response,
)
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=RoleRoutes.BASE.value, tags=[RoleRoutes.tag()])
_logger = get_api_logger()


async def _role_response(session: AsyncSession, role: Role) -> RoleResponse:
    return role_to_response(role, (await permission_names_by_role(session, [role.id])).get(role.id, ()))


@router.get(
    RoleRoutes.ROOT.value,
    response_model=List[RoleResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_roles(
        filters: Annotated[RoleFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW]))
):
    """List roles with filters. include_deleted=true (archived roles too) requires role.archive."""
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, filters, Permissions.ROLE_ARCHIVE)
    query = select(Role)
    query = RoleFilterService.apply_filters(query, filters)
    result = await session.exec(query)

    roles = result.all()
    _logger.info(f"User {current_user.username} listed {len(roles)} roles")

    names = await permission_names_by_role(session, [role.id for role in roles])
    return [role_to_response(role, names.get(role.id, ())) for role in roles]


@router.post(
    RoleRoutes.ROOT.value,
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.ROLE_NAME_TAKEN, ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NAME_ARCHIVED,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def create_role(
        request: Request,
        role_data: RoleCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_CREATE]))
):
    """Create new role. permission_ids may name only permissions the caller holds (403 grant_exceeds_own_permissions)."""
    current_user = auth.user
    # Preventive validation
    await check_role_name_exists(session, role_data.name)
    permissions = [await get_permission_or_404(session, perm_id) for perm_id in role_data.permission_ids or []]
    check_grant(auth, (permission.name for permission in permissions))

    role = Role(**role_data.model_dump(exclude={"permission_ids"}))
    session.add(role)
    async with unique_violation_as(session, ErrorCode.ROLE_NAME_TAKEN):
        await session.flush()

        # Assign permissions if provided
        if role_data.permission_ids is not None:
            for permission in permissions:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))

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

    return await _role_response(session, role)


@router.get(
    RoleRoutes.ALL_PERMISSIONS.value,
    response_model=List[PermissionResponse],
    responses=problems(*GUARDED),
)
async def list_all_permissions(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW]))
):
    """List all available permissions in the system - admin only"""
    current_user = auth.user
    result = await session.exec(
        select(Permission).order_by(Permission.name)
    )

    permissions = result.all()
    _logger.info(f"Admin {current_user.username} listed all {len(permissions)} permissions")

    return [permission_to_response(perm) for perm in permissions]


@router.get(
    RoleRoutes.ROLE_BY_ID.value,
    response_model=RoleResponse,
    responses=problems(ErrorCode.ROLE_NOT_FOUND, *GUARDED),
)
async def get_role(
        role_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW]))
):
    """Get role by ID (administration area)"""
    role = await get_role_or_404(session, role_id, include_deleted=True)

    return await _role_response(session, role)


@router.patch(
    RoleRoutes.ROLE_BY_ID.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.ROLE_NAME_TAKEN, ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND,
        ErrorCode.ROLE_NAME_ARCHIVED, ErrorCode.BUILTIN_ROLE_NOT_RENAMABLE, ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED,
        ErrorCode.ROLE_REQUIRES_ROOT_SCOPE, ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def update_role(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        role_update: RoleUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE]))
):
    """Update role and optionally replace all permissions. Archived roles answer 404 until restored (POST /{role_id}/restore).

    A built-in role keeps its name and its permissions (409); clone it to change them. The caller must hold every permission
    permission_ids adds or removes (403 grant_exceeds_own_permissions). Added permissions apply at once to every
    holder who is not archived: a system permission needs every such assignment of the role at the root (409
    role_requires_root_scope), and the type of every such holder must allow them (409 role_holders_exceed_user_type,
    with the users and their excess_permissions). Nothing is changed on any refusal.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    if role.builtin_key is not None and role_update.name is not None and role_update.name != role.name:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_RENAMABLE)

    # Checked before any change: a refused grant must not leave the other fields saved
    removed = set()
    if role_update.permission_ids is not None:
        wanted_names = {(await get_permission_or_404(session, perm_id)).name for perm_id in role_update.permission_ids}
        current_names = await role_permission_names(session, [role_id])
        if role.builtin_key is not None and wanted_names != current_names:
            raise ApiError(ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED)
        removed = current_names - wanted_names
        check_grant(auth, (wanted_names - current_names) | removed)
        await check_role_permissions_added(session, role_id, wanted_names - current_names)

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

    # Replace all permissions if provided (every id was found above)
    if role_update.permission_ids is not None:
        # Replace by difference: the flush writes INSERTs before DELETEs, so a kept row must not be re-inserted
        wanted = set(role_update.permission_ids)
        current = await session.exec(select(RolePermission).where(RolePermission.role_id == role_id))
        present = set()
        for assignment in current.all():
            if assignment.permission_id in wanted:
                present.add(assignment.permission_id)
            else:
                await session.delete(assignment)
        for perm_id in role_update.permission_ids:
            if perm_id not in present:
                session.add(RolePermission(role_id=role_id, permission_id=perm_id))

        _logger.info(
            f"User {current_user.username} updated role {role.name} with {len(role_update.permission_ids)} permissions"
        )
    else:
        _logger.info(f"User {current_user.username} updated role {role.name}")

    if role_update.name is not None:
        # Two renames to one name pass check_role_name_exists together; the unique index decides
        async with unique_violation_as(session, ErrorCode.ROLE_NAME_TAKEN):
            await session.commit()
    else:
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

    return await _role_response(session, role)


@router.post(
    RoleRoutes.ROLE_ARCHIVE.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.BUILTIN_ROLE_NOT_ARCHIVABLE, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS,
        *GUARDED,
    ),
)
async def archive_role(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_ARCHIVE]))
):
    """Archive a role (is_deleted=true). Nothing is removed: user assignments and permissions stay in the database.

    An archived role stops granting permissions immediately, cannot be assigned and is hidden from lists
    unless include_deleted=true. Undo with POST /{role_id}/restore. Already archived or unknown: 404.
    The caller must hold every permission of the role (it is revoked from every holder, 403). Built-in roles (the
    administrator is the built-in admin role at the root) are never archived (409).
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    if role.builtin_key is not None:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_ARCHIVABLE)
    granted = await role_permission_names(session, [role_id])
    check_grant(auth, granted)

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
    return await _role_response(session, role)


@router.post(
    RoleRoutes.ROLE_RESTORE.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_NOT_ARCHIVED, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def restore_role(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_ARCHIVE]))
):
    """Restore an archived role (is_deleted=false): it grants its permissions again and can be assigned. Not archived: 409.
    The caller must hold every permission of the role (403 grant_exceeds_own_permissions)."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_NOT_ARCHIVED)
    check_grant(auth, await role_permission_names(session, [role_id]))

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
    return await _role_response(session, role)


@router.post(
    RoleRoutes.ROLE_CLONE.value,
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_NAME_TAKEN, ErrorCode.ROLE_NAME_ARCHIVED,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def clone_role(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        role_data: RoleClone,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_CREATE]))
):
    """Create a custom role with the permissions of an existing one (a built-in role is the usual source).

    The copy is an ordinary role: it can be renamed, changed, archived and deleted. The caller must hold every
    permission of the source (403 grant_exceeds_own_permissions).
    """
    current_user = auth.user
    source = await get_role_or_404(session, role_id)
    await check_role_name_exists(session, role_data.name)
    names = await role_permission_names(session, [source.id])
    check_grant(auth, names)
    source_id = source.id

    role = Role(**role_data.model_dump())
    session.add(role)
    async with unique_violation_as(session, ErrorCode.ROLE_NAME_TAKEN):
        await session.flush()
        grants = await session.exec(select(RolePermission.permission_id).where(RolePermission.role_id == source_id))
        for permission_id in grants.all():
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await session.commit()
    await session.refresh(role)

    await create_audit_log(
        session=session,
        action="create",
        resource_type="role",
        resource_id=role.id,
        user_id=current_user.id,
        new_values=role.model_dump(),
        details={
            "message": f"User {current_user.username} cloned role {source_id} as '{role.name}'",
            "cloned_from": source_id,
        },
        **client_info(request, auth)
    )
    await session.commit()
    _logger.info(f"User {current_user.username} cloned role {source_id} as {role.name}")
    return role_to_response(role, names)


@router.delete(
    RoleRoutes.ROLE_BY_ID.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.BUILTIN_ROLE_NOT_DELETABLE, ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE,
        ErrorCode.ROLE_HAS_ACTIVE_USERS, *GUARDED,
    ),
)
async def delete_role(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_DELETE]))
):
    """PERMANENTLY delete a role. This is the only hard delete in the API and cannot be undone.

    Requires role.delete (distinct from role.archive). The role row, its permission grants and its
    assignments to archived users are removed from the database. Preconditions, each answered with 409:
    the role must be archived first (POST /{role_id}/archive), no active user may still hold it
    (the response lists them), and a built-in role can never be deleted.
    The audit log keeps a full copy of the role, its permissions and its assignments.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True)
    if role.builtin_key is not None:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_DELETABLE)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE)

    assigned = await session.exec(
        select(User.id, User.username, User.is_deleted)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(RoleAssignment.role_id == role_id)
        .distinct()
        .order_by(User.id)
    )
    assigned_rows = assigned.all()
    active_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if not deleted]
    if active_users:
        raise ApiError(ErrorCode.ROLE_HAS_ACTIVE_USERS, assigned_users=active_users)
    archived_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if deleted]
    # A full copy of every role_assignments row the delete removes: every column
    assignments = [assignment.model_dump() for assignment in (await session.exec(
        select(RoleAssignment).where(RoleAssignment.role_id == role_id).order_by(RoleAssignment.id)
    )).all()]

    permissions = await session.exec(
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
        .order_by(Permission.name)
    )
    permission_names = list(permissions.all())
    old_values = {
        **role.model_dump(), "permissions": permission_names, "assigned_archived_users": archived_users,
        "assignments": assignments,
    }

    # Core statements on purpose: no ORM cascade (no lazy loads in an async session), every row is removed explicitly
    await session.execute(sa_delete(RolePermission).where(RolePermission.role_id == role_id))
    await session.execute(sa_delete(RoleAssignment).where(RoleAssignment.role_id == role_id))
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
                f"({len(permission_names)} permissions, {len(assignments)} assignments of {len(archived_users)} "
                f"archived users)"
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
    responses=problems(ErrorCode.ROLE_NOT_FOUND, *GUARDED),
)
async def list_role_permissions(
        role_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW]))
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

    return [permission_to_response(perm) for perm in permissions]


async def _role_permissions(session: AsyncSession, role_id: Annotated[int, Path(ge=1)]) -> List[PermissionResponse]:
    result = await session.exec(
        select(Permission).join(RolePermission, RolePermission.permission_id == Permission.id).where(RolePermission.role_id == role_id)
    )
    return [permission_to_response(perm) for perm in result.all()]


@router.put(
    RoleRoutes.ROLE_PERMISSION_BY_ID.value,
    response_model=List[PermissionResponse],
    responses=problems(
        ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND, ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED,
        ErrorCode.ROLE_REQUIRES_ROOT_SCOPE, ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def add_role_permission(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        permission_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE]))
):
    """Grant one permission to a role (idempotent). Returns the role's permissions.
    Only a permission the caller holds can be granted (403 grant_exceeds_own_permissions); a built-in role gets
    permissions only by seeding (409 builtin_role_permissions_fixed). The permission applies at once to every holder
    who is not archived: a system permission needs every such assignment of the role at the root (409
    role_requires_root_scope), and the type of every such holder must allow it (409 role_holders_exceed_user_type)."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    permission = await get_permission_or_404(session, permission_id)

    existing = await session.exec(
        select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
    )
    if not existing.first():
        if role.builtin_key is not None:
            raise ApiError(ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED)
        check_grant(auth, [permission.name])
        await check_role_permissions_added(session, role_id, [permission.name])
        # A rollback expires every loaded object: keep what is needed afterwards as plain values
        actor_id, actor_name, role_name, permission_name = current_user.id, current_user.username, role.name, permission.name
        session.add(RolePermission(role_id=role_id, permission_id=permission_id))
        role.updated_at = datetime.now(timezone.utc)
        session.add(role)
        try:
            await session.commit()
        except IntegrityError as exc:
            # A concurrent request granted it between the check and the insert: the PUT is idempotent, so answer
            # as if the permission had been there (the other request wrote the audit row)
            await session.rollback()
            if not is_unique_violation(exc):
                raise
        else:
            await create_audit_log(
                session=session,
                action="update",
                resource_type="role",
                resource_id=role_id,
                user_id=actor_id,
                new_values={"permission_added": permission_name},
                details={"message": f"User {actor_name} granted '{permission_name}' to role '{role_name}'"},
                **client_info(request, auth)
            )
            await session.commit()
            _logger.info(f"User {actor_name} granted {permission_name} to role {role_name}")

    return await _role_permissions(session, role_id)


@router.delete(
    RoleRoutes.ROLE_PERMISSION_BY_ID.value,
    response_model=List[PermissionResponse],
    responses=problems(
        ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_PERMISSION_NOT_ASSIGNED,
        ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def remove_role_permission(
        request: Request,
        role_id: Annotated[int, Path(ge=1)],
        permission_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_UPDATE]))
):
    """Revoke one permission from a role. Returns the role's remaining permissions.
    Only a permission the caller holds can be revoked (403); a built-in role keeps its permissions (409)."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id)
    permission = await get_permission_or_404(session, permission_id)

    existing = await session.exec(
        select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
    )
    row = existing.first()
    if not row:
        raise ApiError(ErrorCode.ROLE_PERMISSION_NOT_ASSIGNED)
    if role.builtin_key is not None:
        raise ApiError(ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED)
    check_grant(auth, [permission.name])

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
