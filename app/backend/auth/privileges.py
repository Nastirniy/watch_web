"""Delegation rules of the administration routes for roles and users (spec section 6).

Nobody becomes able to do more than they could by changing access:

- check_grant: role edits (permissions added to or removed from a role, a role archived, restored or cloned) need
  every permission the change grants or revokes, held anywhere (role.* are system permissions: only holders at the
  root edit roles). 403 grant_exceeds_own_permissions with the permissions the caller lacks.
- check_grant_at: an assignment at a node (a new user's default assignment, the assignment API) needs every permission
  of the role covered at that node (AuthContext.covers). 403 grant_exceeds_own_permissions. check_assign_at (the
  assignment API, create and delete) also needs user.update covered at the node, so a role without permissions
  cannot be placed or removed outside the caller's reach.
- check_target_user: a change to another user (fields, home post, sessions, archive state) needs the route's user.*
  permission covering the user (their home post, the root for a user without one) and that the caller could grant
  every assignment the user has (each role's permissions covered at its node), assignments of archived roles
  included (they come back with the role's restore). 403 target_user_more_privileged.
- check_an_admin_remains / check_admin_kept: run after the change is written to the session (flushed here, committed
  by the route): the caller has not taken the built-in admin role at the root from themselves (409
  cannot_remove_own_admin, check_admin_kept, answered first; whether they held it is read with holds_admin_at_root
  before the change), and an active, non-archived user still holds it (409 last_admin_required). The administrator
  is the built-in admin role at the root (_admin_at_root), nothing else.

What a user holds is context.grant_rows, the same definition as their AuthContext (check_target_user reads it with
the archived roles too).
"""
from typing import FrozenSet, Iterable, List, Set, Tuple, Union

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import AuthContext, grant_rows, grants_of, permission_name
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions, UserType
from app.backend.errors.problems import ApiError
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
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
    """The caller holds every permission the change grants or revokes (anywhere: role edits)"""
    missing = sorted(set(permissions) - auth.permissions)
    if missing:
        logger.warning(f"User {auth.user.username} tried to grant or revoke permissions they lack: {missing}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=missing)


def _not_covered(auth: AuthContext, permissions: Iterable[str], node: Node) -> Set[str]:
    return {name for name in set(permissions) if not auth.covers(name, node)}


def check_grant_at(auth: AuthContext, permissions: Iterable[str], node: Node) -> None:
    """The caller holds every permission at the node (inside a subtree where it is held): they may assign a role with
    these permissions there"""
    missing = sorted(_not_covered(auth, permissions, node))
    if missing:
        logger.warning(f"User {auth.user.username} tried to grant at {node.key} permissions they lack there: {missing}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=missing)


def check_assign_at(auth: AuthContext, permissions: Iterable[str], node: Node) -> None:
    """An assignment made or removed at the node through the assignment API: user.update covered at the node itself
    and every permission of the role there, in one 403 grant_exceeds_own_permissions whose missing lists both. A role
    without permissions is no free pass: the node must lie inside the caller's user.update."""
    check_grant_at(auth, {*permissions, Permissions.USER_UPDATE.value}, node)


async def _every_assignment(session: AsyncSession, user_id: int) -> Tuple[Grant, ...]:
    """Every assignment of the user as a grant, those of archived roles included: an archived role grants nothing
    now (grant_rows, AuthContext) but its assignments come back with the role's restore"""
    return grants_of((await session.exec(grant_rows(user_id, include_archived_roles=True))).all())


async def check_target_user(session: AsyncSession, auth: AuthContext, target: User,
                            permission: Union[Permissions, str] = Permissions.USER_UPDATE) -> None:
    """The caller's permission covers the target user, and the caller could grant every existing assignment of the
    target, archived roles included (the caller themselves always could)"""
    name = permission_name(permission)
    missing: List[str] = []
    if not auth.covers_user(name, target):
        missing = [name]
    elif target.id != auth.user.id:
        lacking: Set[str] = set()
        for grant in await _every_assignment(session, target.id):
            lacking |= _not_covered(auth, grant.permissions, grant.node)
        missing = sorted(lacking)
    if missing:
        logger.warning(f"User {auth.user.username} tried to change user {target.id} beyond their reach: {missing}")
        raise access_denied(AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, missing=missing)


def _admin_at_root():
    """Assignments of the built-in admin role at the root: the one definition of the administrator"""
    return (
        select(RoleAssignment.user_id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(Role.builtin_key == UserType.ADMIN.value, Role.is_deleted == False,
               RoleAssignment.scope_key == ROOT.key)
    )


async def holds_admin_at_root(session: AsyncSession, user_id: int) -> bool:
    """The user holds the built-in admin role at the root (as written to the session: flush first when it changed)"""
    return (await session.exec(_admin_at_root().where(RoleAssignment.user_id == user_id).limit(1))).first() is not None


async def check_an_admin_remains(session: AsyncSession) -> None:
    """After the change: an active, non-archived user holds the built-in admin role at the root"""
    await session.flush()
    admin = (await session.exec(
        _admin_at_root().join(User, User.id == RoleAssignment.user_id)
        .where(User.is_active == True, User.is_deleted == False)
        .limit(1)
    )).first()
    if admin is None:
        logger.warning("Refused a change that would leave no active administrator")
        raise ApiError(ErrorCode.LAST_ADMIN_REQUIRED)


async def check_admin_kept(session: AsyncSession, auth: AuthContext, held_before: bool) -> None:
    """After a change that may take the admin role at the root from its holders: the caller, who held it before the
    change (held_before: holds_admin_at_root read before it), still holds it (checked first: the answer to an
    administrator taking it from themselves, the last one included), and someone keeps it"""
    await session.flush()
    if held_before and not await holds_admin_at_root(session, auth.user.id):
        logger.warning(f"User {auth.user.username} tried to remove the admin role at the root from themselves")
        raise ApiError(ErrorCode.CANNOT_REMOVE_OWN_ADMIN)
    await check_an_admin_remains(session)
