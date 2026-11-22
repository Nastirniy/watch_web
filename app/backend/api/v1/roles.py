from contextlib import nullcontext
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
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.assignments import check_role_permissions_added
from app.backend.auth.integrity import other_level
from app.backend.auth.privileges import check_grant, check_role_grant, role_nodes, role_permission_names
from app.backend.enums.general import Permissions, RoleKind
from app.backend.enums.routes import RoleRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.api.v1.models.query.roles import RoleFilterParams
from app.backend.filters.role_filter import RoleFilterService
from app.backend.api.shared.helpers.roles_helper import (
    get_role_or_404,
    permission_names_by_role,
    get_permission_or_404,
    check_role_name_exists,
    delete_legacy_holder_rows,
    legacy_holder_rows,
)
from app.backend.api.v1.models.request.roles import RoleClone, RoleCreate, RoleUpdate
from app.backend.api.v1.models.response.roles import (
    RoleResponse, PermissionResponse, role_to_response, permission_to_response,
)
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems
from app.backend.services.events import publish_role_changed

router = APIRouter(prefix=RoleRoutes.BASE.value, tags=[RoleRoutes.tag()])
_logger = get_api_logger()


def _refuse_other_level(kind, names) -> None:
    """A role holds permissions of its kind's level only (409 permission_level_mismatch)"""
    names = list(names)
    mismatched = other_level(kind, names)
    if mismatched:
        raise ApiError(ErrorCode.PERMISSION_LEVEL_MISMATCH, role_kind=RoleKind(kind).value, permissions=mismatched)


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
        ErrorCode.PERMISSION_LEVEL_MISMATCH, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def create_role(
        request: Request,
        role_data: RoleCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW, Permissions.ROLE_CREATE]))
):
    """Create new role of a kind (tree or system). permission_ids name permissions of the kind's level only (409
    permission_level_mismatch) and only permissions the caller holds (403 grant_exceeds_own_permissions)."""
    current_user = auth.user
    # Preventive validation
    await check_role_name_exists(session, role_data.name)
    permissions = [await get_permission_or_404(session, perm_id) for perm_id in role_data.permission_ids or []]
    _refuse_other_level(role_data.kind, (permission.name for permission in permissions))
    check_grant(auth, (permission.name for permission in permissions))

    role = Role(**role_data.model_dump(exclude={"permission_ids", "kind"}), kind=role_data.kind.value)
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

        # One transaction: the role, its permissions, its audit row
        await session.flush()
        await session.refresh(role)
        await create_audit_log(
            session, AuditEvent.ROLE_CREATE,
            params={"role_name": role.name, "role_kind": role.kind},
            resource_id=role.id,
            user_id=current_user.id,
            new_values=role.model_dump(),
            **client_info(request, auth)
        )
        publish_role_changed(session, role.id)
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
    """List every permission of the catalog with its level (system: assignable only at the root)"""
    current_user = auth.user
    result = await session.exec(
        select(Permission).order_by(Permission.name)
    )

    permissions = result.all()
    _logger.info(f"User {current_user.username} listed all {len(permissions)} permissions")

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
    """Get a role by id, archived roles included"""
    role = await get_role_or_404(session, role_id, include_deleted=True)

    return await _role_response(session, role)


@router.patch(
    RoleRoutes.ROLE_BY_ID.value,
    response_model=RoleResponse,
    responses=problems(
        ErrorCode.ROLE_NAME_TAKEN, ErrorCode.PERMISSION_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND,
        ErrorCode.ROLE_NAME_ARCHIVED, ErrorCode.BUILTIN_ROLE_NOT_RENAMABLE, ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED,
        ErrorCode.PERMISSION_LEVEL_MISMATCH, ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE,
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

    A built-in role keeps its name and its permissions (409); clone it to change them. Permissions permission_ids
    removes must be held by the caller (at some node). Permissions it adds apply at once at every assignment of the
    role, those of archived users included (restoring a user gives the role back as it is), so the caller must hold
    each of them at every node the role is assigned at (at some node for a role assigned nowhere): otherwise 403
    grant_exceeds_own_permissions with the permissions missing. permission_ids name permissions of the role's level
    only (409 permission_level_mismatch). Then the type of every holder, archived ones included, must allow the added
    permissions (409 role_holders_exceed_user_type, with the users and their excess_permissions). Nothing is changed
    on any refusal.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id, lock=True)
    if role.builtin_key is not None and role_update.name is not None and role_update.name != role.name:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_RENAMABLE)

    # Checked before any change: a refused grant must not leave the other fields saved
    removed = set()
    if role_update.permission_ids is not None:
        wanted_names = {(await get_permission_or_404(session, perm_id)).name for perm_id in role_update.permission_ids}
        current_names = await role_permission_names(session, [role_id])
        if role.builtin_key is not None and wanted_names != current_names:
            raise ApiError(ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED)
        _refuse_other_level(role.kind, wanted_names)
        added, removed = wanted_names - current_names, current_names - wanted_names
        check_role_grant(auth, await role_nodes(session, role_id), added=added, removed=removed)
        await check_role_permissions_added(session, role_id, added)

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

    # One transaction: the change, its audit row, one commit. Two renames to one name pass check_role_name_exists
    # together; the unique index decides, and only a rename can hit it
    guard = unique_violation_as(session, ErrorCode.ROLE_NAME_TAKEN) if role_update.name is not None else nullcontext()
    async with guard:
        await session.flush()
        await session.refresh(role)
        await create_audit_log(
            session, AuditEvent.ROLE_UPDATE,
            params={"role_name": role.name, "role_kind": role.kind},
            resource_id=role.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values=role.model_dump(),
            **client_info(request, auth)
        )
        publish_role_changed(session, role.id)
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
    role = await get_role_or_404(session, role_id, lock=True)
    if role.builtin_key is not None:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_ARCHIVABLE)
    granted = await role_permission_names(session, [role_id])
    check_grant(auth, granted)

    old_values = role.model_dump()
    role.is_deleted = True
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await create_audit_log(
        session, AuditEvent.ROLE_ARCHIVE,
        params={"role_name": role.name, "role_kind": role.kind},
        resource_id=role.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=role.model_dump(),
        **client_info(request, auth),
    )
    publish_role_changed(session, role.id)
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
    The role's permissions apply again at every node it is assigned at (assignments of archived users included), so the
    caller must hold every permission of the role at each of these nodes (at some node for a role assigned nowhere):
    otherwise 403 grant_exceeds_own_permissions with the permissions missing."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True, lock=True)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_NOT_ARCHIVED)
    check_role_grant(auth, await role_nodes(session, role_id), added=await role_permission_names(session, [role_id]))

    old_values = role.model_dump()
    role.is_deleted = False
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await create_audit_log(
        session, AuditEvent.ROLE_RESTORE,
        params={"role_name": role.name, "role_kind": role.kind},
        resource_id=role.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=role.model_dump(),
        **client_info(request, auth),
    )
    publish_role_changed(session, role.id)
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
    source_id, source_name = source.id, source.name

    role = Role(**role_data.model_dump(), kind=source.kind)
    session.add(role)
    async with unique_violation_as(session, ErrorCode.ROLE_NAME_TAKEN):
        await session.flush()
        grants = await session.exec(select(RolePermission.permission_id).where(RolePermission.role_id == source_id))
        for permission_id in grants.all():
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await session.flush()
        await session.refresh(role)
        await create_audit_log(
            session, AuditEvent.ROLE_CLONE,
            params={"role_name": role.name, "role_kind": role.kind, "cloned_from": source_name,
                    "cloned_from_id": source_id},
            resource_id=role.id,
            user_id=current_user.id,
            new_values=role.model_dump(),
            **client_info(request, auth)
        )
        publish_role_changed(session, role.id)
        await session.commit()
    _logger.info(f"User {current_user.username} cloned role {source_id} as {role.name}")
    return role_to_response(role, names)


@router.delete(
    RoleRoutes.ROLE_BY_ID.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.ROLE_NOT_FOUND, ErrorCode.BUILTIN_ROLE_NOT_DELETABLE, ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE,
        ErrorCode.ROLE_HAS_ACTIVE_USERS, ErrorCode.LAST_ASSIGNMENT_REQUIRED, *GUARDED,
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
    assignments to archived users are removed from the database, and so are its rows in user_roles_v1 (the legacy
    holders migration 023 keeps until a cleanup revision; a downgrade does not bring them or the role back).
    Preconditions, each answered with 409: the role must be archived first (POST /{role_id}/archive), no active user
    may still hold it (the response lists them), no archived holder may be left without an assignment (a restore
    would bring the user back without any: last_assignment_required; give them another one first), and a built-in
    role can never be deleted.
    The audit log keeps a full copy of the role, its permissions, its assignments and its user_roles_v1 rows.
    """
    current_user = auth.user
    role = await get_role_or_404(session, role_id, include_deleted=True, lock=True)
    if role.builtin_key is not None:
        raise ApiError(ErrorCode.BUILTIN_ROLE_NOT_DELETABLE)
    if not role.is_deleted:
        raise ApiError(ErrorCode.ROLE_DELETE_REQUIRES_ARCHIVE)

    assigned = await session.exec(
        select(User.id, User.username, User.is_deleted)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(RoleAssignment.role_id == role_id)
        .union(
            select(User.id, User.username, User.is_deleted)
            .join(SystemRoleAssignment, SystemRoleAssignment.user_id == User.id)
            .where(SystemRoleAssignment.role_id == role_id))
    )
    assigned_rows = sorted(assigned.all())
    active_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if not deleted]
    if active_users:
        raise ApiError(ErrorCode.ROLE_HAS_ACTIVE_USERS, assigned_users=active_users)
    archived_users = [{"id": uid, "username": name} for uid, name, deleted in assigned_rows if deleted]
    # Spec section 6 rule 6: every archived holder keeps another assignment (of any role: a restore gives it back)
    holder_ids = [user["id"] for user in archived_users]
    kept = set(row[0] for row in (await session.exec(
        select(RoleAssignment.user_id).where(RoleAssignment.user_id.in_(holder_ids), RoleAssignment.role_id != role_id)
        .union(select(SystemRoleAssignment.user_id).where(SystemRoleAssignment.user_id.in_(holder_ids),
                                                          SystemRoleAssignment.role_id != role_id))
    )).all()) if holder_ids else set()
    stranded = [user for user in archived_users if user["id"] not in kept]
    if stranded:
        _logger.warning(f"User {current_user.username} tried to delete role {role_id}, the only assignment of archived "
                        f"users {[user['id'] for user in stranded]}")
        raise ApiError(ErrorCode.LAST_ASSIGNMENT_REQUIRED)
    # A full copy of every role_assignments row the delete removes: every column
    assignments = [assignment.model_dump() for assignment in (await session.exec(
        select(RoleAssignment).where(RoleAssignment.role_id == role_id).order_by(RoleAssignment.id)
    )).all()]
    system_assignments = [assignment.model_dump() for assignment in (await session.exec(
        select(SystemRoleAssignment).where(SystemRoleAssignment.role_id == role_id).order_by(SystemRoleAssignment.id)
    )).all()]

    permissions = await session.exec(
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
        .order_by(Permission.name)
    )
    permission_names = list(permissions.all())
    legacy_holders = await legacy_holder_rows(session, role_id)
    old_values = {
        **role.model_dump(), "permissions": permission_names, "assigned_archived_users": archived_users,
        "assignments": assignments, "system_assignments": system_assignments, "user_roles_v1": legacy_holders,
    }

    # Core statements on purpose: no ORM cascade (no lazy loads in an async session), every row is removed explicitly
    await session.execute(sa_delete(RolePermission).where(RolePermission.role_id == role_id))
    await session.execute(sa_delete(RoleAssignment).where(RoleAssignment.role_id == role_id))
    await session.execute(sa_delete(SystemRoleAssignment).where(SystemRoleAssignment.role_id == role_id))
    await delete_legacy_holder_rows(session, role_id)
    await session.execute(sa_delete(Role).where(Role.id == role_id))
    await create_audit_log(
        session, AuditEvent.ROLE_DELETE,
        params={"role_name": old_values["name"], "role_kind": old_values["kind"]},
        resource_id=role_id,
        user_id=current_user.id,
        old_values=old_values,
        **client_info(request, auth),
    )
    publish_role_changed(session, role_id)
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
        ErrorCode.PERMISSION_LEVEL_MISMATCH, ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE,
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
    A built-in role gets permissions only by seeding (409 builtin_role_permissions_fixed). The permission applies at
    once at every assignment of the role, those of archived users included (restoring a user gives the role back as it
    is), so the caller must hold it at every node the role is assigned at (at some node for a role assigned nowhere):
    otherwise 403 grant_exceeds_own_permissions. The permission must be of the role's level (409
    permission_level_mismatch), and the type of every holder, archived ones included, must allow it (409
    role_holders_exceed_user_type)."""
    current_user = auth.user
    role = await get_role_or_404(session, role_id, lock=True)
    permission = await get_permission_or_404(session, permission_id)

    existing = await session.exec(
        select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
    )
    if not existing.first():
        if role.builtin_key is not None:
            raise ApiError(ErrorCode.BUILTIN_ROLE_PERMISSIONS_FIXED)
        _refuse_other_level(role.kind, [permission.name])
        check_role_grant(auth, await role_nodes(session, role_id), added=[permission.name])
        await check_role_permissions_added(session, role_id, [permission.name])
        # A rollback expires every loaded object: keep what is needed afterwards as plain values
        actor_id, actor_name, role_name, role_kind, permission_name = (
            current_user.id, current_user.username, role.name, role.kind, permission.name)
        session.add(RolePermission(role_id=role_id, permission_id=permission_id))
        role.updated_at = datetime.now(timezone.utc)
        session.add(role)
        try:
            # One transaction: the grant and its audit row
            await session.flush()
            await create_audit_log(
                session, AuditEvent.ROLE_PERMISSION_ADD,
                params={"role_name": role_name, "role_kind": role_kind, "permission": permission_name},
                resource_id=role_id,
                user_id=actor_id,
                new_values={"permission_added": permission_name},
                **client_info(request, auth)
            )
            publish_role_changed(session, role_id)
            await session.commit()
        except IntegrityError as exc:
            # A concurrent request granted it between the check and the insert: the PUT is idempotent, so answer
            # as if the permission had been there (that request wrote the audit row; the rollback drops ours)
            await session.rollback()
            if not is_unique_violation(exc):
                raise
        else:
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
    role = await get_role_or_404(session, role_id, lock=True)
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
    await create_audit_log(
        session, AuditEvent.ROLE_PERMISSION_REMOVE,
        params={"role_name": role.name, "role_kind": role.kind, "permission": permission.name},
        resource_id=role.id,
        user_id=current_user.id,
        old_values={"permission_removed": permission.name},
        **client_info(request, auth)
    )
    publish_role_changed(session, role.id)
    await session.commit()
    _logger.info(f"User {current_user.username} revoked {permission.name} from role {role.name}")

    return await _role_permissions(session, role_id)
