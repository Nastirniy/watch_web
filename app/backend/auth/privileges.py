"""Privilege rules of the administration routes for roles and users.

admin.true opens the administration area; these rules keep it from being a way to more permissions:

- check_grant: the caller holds every permission a change grants or revokes (roles assigned to a user,
  permissions added to or removed from a role, a role archived or restored for its holders). 403
  grant_exceeds_own_permissions with the permissions the caller lacks.
- check_target_user: the caller writes to another user (any field, post access, archive state, sessions) only
  when that user holds no permission the caller lacks. 403 target_user_more_privileged.
- check_an_admin_remains / check_admin_kept: run after the change is written to the session (flushed here,
  committed by the route): an active, non-archived user still holds admin.true through a role that is not
  archived (409 last_admin_required), and the caller has not taken admin.true from themselves (409
  cannot_remove_own_admin).

The seeded admin role holds every permission (its name and permission set are fixed, the role routes refuse to
change them), so its holders pass every grant and target check. What a user holds is context.held_permissions,
the same definition as their AuthContext.
"""
from typing import FrozenSet, Iterable

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import AuthContext, effective_permissions, held_permissions
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.errors.problems import ApiError
from app.backend.models.role import Permission, RolePermission, UserRole
from app.backend.models.user import User


async def role_permission_names(session: AsyncSession, role_ids: Iterable[int]) -> FrozenSet[str]:
    """Names of the permissions the given roles hold"""
    ids = list(role_ids)
    if not ids:
        return frozenset()
    rows = await session.exec(
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(ids))
    )
    return frozenset(rows.all())


def check_grant(auth: AuthContext, permissions: Iterable[str]) -> None:
    """The caller holds every permission the change grants or revokes"""
    missing = sorted(set(permissions) - auth.permissions)
    if missing:
        logger.warning(f"User {auth.user.username} tried to grant or revoke permissions they lack: {missing}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=missing)


async def check_target_user(session: AsyncSession, auth: AuthContext, target: User) -> None:
    """The target user holds no permission the caller lacks"""
    if target.id == auth.user.id:
        return
    missing = sorted(await effective_permissions(session, target.id) - auth.permissions)
    if missing:
        logger.warning(f"User {auth.user.username} tried to change user {target.id}, who holds more: {missing}")
        raise access_denied(AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, missing=missing)


async def check_an_admin_remains(session: AsyncSession) -> None:
    """After the change: an active, non-archived user holds admin.true through a role that is not archived"""
    await session.flush()
    admin = (await session.exec(
        held_permissions(UserRole.user_id)
        .join(User, User.id == UserRole.user_id)
        .where(User.is_active == True, User.is_deleted == False, Permission.name == Permissions.IS_ADMIN.value)
        .limit(1)
    )).first()
    if admin is None:
        logger.warning("Refused a change that would leave no active administrator")
        raise ApiError(ErrorCode.LAST_ADMIN_REQUIRED)


async def check_admin_kept(session: AsyncSession, auth: AuthContext) -> None:
    """After a change that may take admin.true from its holders: someone keeps it, and the caller keeps theirs"""
    await check_an_admin_remains(session)
    if auth.is_admin and Permissions.IS_ADMIN.value not in await effective_permissions(session, auth.user.id):
        logger.warning(f"User {auth.user.username} tried to remove admin.true from themselves")
        raise ApiError(ErrorCode.CANNOT_REMOVE_OWN_ADMIN)
