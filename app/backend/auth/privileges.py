"""Delegation rules of the administration routes for roles and users (spec section 6).

Nobody becomes able to do more than they could by changing access:

- check_grant: role edits that grant nothing new where a role is held (a role created or cloned, permissions removed
  from a role, a role archived) need every permission the change revokes or copies, held anywhere (role.* are system
  permissions: only holders at the root edit roles). 403 grant_exceeds_own_permissions with the permissions the
  caller lacks.
- check_role_grant: permissions added to a role (PATCH permission_ids, PUT of one permission) and the permissions of a
  restored role apply at once at every node the role is assigned at (role_nodes: assignments of archived users
  included, their restore gives the role back as it is), so the caller holds each of them at every such node
  (check_grant_at at each); a role assigned nowhere: held anywhere (check_grant). Removed permissions: held anywhere.
  One 403 grant_exceeds_own_permissions listing every permission lacking (an added one lacking at any node).
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
  is a system assignment of the built-in system_administrator role (_admin_at_root), nothing else.
- lock_administrators: the first statement of every write that may take that role from someone (an assignment
  removed, a user deactivated, archived or given another type). Such writes run one after another, so the count of
  check_an_admin_remains sees the one before: two administrators deactivating each other at once can no longer
  both succeed.

What a user holds is context.load_grants, the same definition as their AuthContext (check_target_user reads it with
the archived roles too).
"""
from typing import FrozenSet, Iterable, List, Set, Tuple, Union

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import AuthContext, load_grants, permission_name
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import BuiltinRole, Permissions
from app.backend.errors.problems import ApiError
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.utils.row_locks import lock_first


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
    """The caller holds every permission the change revokes or copies, anywhere (a role created or cloned, a role's
    removals, its archive)"""
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


async def role_nodes(session: AsyncSession, role_id: int) -> FrozenSet[Node]:
    """Every node the role is assigned at, assignments of archived users included (R28)"""
    keys = await session.exec(select(RoleAssignment.scope_key).where(RoleAssignment.role_id == role_id).distinct())
    return frozenset(Node.parse(key) for key in keys.all())


def check_role_grant(auth: AuthContext, nodes: Iterable[Node], added: Iterable[str] = (),
                     removed: Iterable[str] = ()) -> None:
    """A role edit (spec section 6 rule 3): the permissions removed held anywhere; the permissions added (a restore:
    every permission of the role) held at every node the role is assigned at, anywhere for a role assigned nowhere"""
    nodes, added = frozenset(nodes), set(added)
    missing = set(removed) - auth.permissions
    if nodes:
        for node in nodes:
            missing |= _not_covered(auth, added, node)
    else:
        missing |= added - auth.permissions
    if missing:
        logger.warning(f"User {auth.user.username} tried to change a role held at {sorted(n.key for n in nodes)} "
                       f"with permissions they lack there: {sorted(missing)}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=sorted(missing))


def check_assign_at(auth: AuthContext, permissions: Iterable[str], node: Node) -> None:
    """An assignment made or removed at the node through the assignment API: user.update covered at the node itself
    and every permission of the role there, in one 403 grant_exceeds_own_permissions whose missing lists both. A role
    without permissions is no free pass: the node must lie inside the caller's user.update."""
    check_grant_at(auth, {*permissions, Permissions.USER_UPDATE.value}, node)


async def _every_assignment(session: AsyncSession, user: User) -> Tuple[Grant, ...]:
    """Every assignment of the user as a grant, system assignments (at the root) and those of archived roles
    included: an archived role grants nothing now (grant_rows, AuthContext) but its assignments come back with the
    role's restore"""
    return await load_grants(session, user, include_archived_roles=True)


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
        for grant in await _every_assignment(session, target):
            lacking |= _not_covered(auth, grant.permissions, grant.node)
        missing = sorted(lacking)
    if missing:
        logger.warning(f"User {auth.user.username} tried to change user {target.id} beyond their reach: {missing}")
        raise access_denied(AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, missing=missing)


def _admin_at_root():
    """System assignments of the built-in system_administrator role: the one definition of the administrator"""
    return (
        select(SystemRoleAssignment.user_id)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .where(Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value, Role.is_deleted == False)
    )


async def lock_administrators(session: AsyncSession) -> None:
    """The first statement of a write that may take the built-in system_administrator role from someone: that role's
    row locked (utils/row_locks.py). Later locks of the write (the user) use row_locks.lock."""
    await lock_first(session, select(Role).where(Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value))


async def holds_admin_at_root(session: AsyncSession, user_id: int) -> bool:
    """The user holds the built-in system_administrator role (as written to the session: flush first when it
    changed)"""
    return (await session.exec(
        _admin_at_root().where(SystemRoleAssignment.user_id == user_id).limit(1))).first() is not None


async def check_an_admin_remains(session: AsyncSession) -> None:
    """After the change: an active, non-archived user holds the built-in admin role at the root"""
    await session.flush()
    admin = (await session.exec(
        _admin_at_root().join(User, User.id == SystemRoleAssignment.user_id)
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
