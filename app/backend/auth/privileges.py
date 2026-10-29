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
- check_target_user: a change to a user (fields, home post, sessions, archive state, assignments, certificates) needs
  the user record visible to the caller first (check_user_view: user.view covering the user, the same 403
  user_self_only as GET /users/{id}, so a write tells nothing a read would not), then the route's user.* permission
  covering the user (their home post, the root for a user without one) and, for another user, that the caller could
  grant every assignment the user has: each role's permissions, as the role holds them (every_assignment: not
  through the target's boundary, which a type change widens), covered at its node, assignments of archived roles
  included (they come back with the role's restore). 403 target_user_more_privileged.
- check_boundary_change: a type change puts in effect what the new boundary lets through of the target's existing
  assignments; whatever it switches on that the old boundary left out needs the caller covering it at the
  assignment's node (403 grant_exceeds_own_permissions). For another user check_target_user already asked for every
  permission of every assignment; this rule also holds for a change of oneself.
- check_move / check_node_restore: the scope tree is access too. A move of a post or a group (PUT /posts/{id}
  group_id, PATCH /scope-groups/{id} parent_id, PATCH /brigades/{id} corps_id) puts every grant at the destination's
  ancestors that are not already above the moved node in effect at the moved subtree; the restore of an archived
  post or group (scope group, corps, brigade) revives every grant at that node. Both equal assigning those roles at
  the node, so both need what the assignment API needs (rule 1): the caller holds every permission of each such
  assignment's role at the moved or restored node, before the change (check_grant_at, 403
  grant_exceeds_own_permissions with every permission lacking).
  Decision (2026-10-29, audit H1): the grants of every user count, not only the caller's. Moving a post under the
  administrator of another brigade gives that administrator the post, its users and its data; the assignment API
  would refuse the mover that grant, so the move is refused too ("nobody becomes able to do more than they could
  by changing access" covers giving access to others). The caller's own grants are the same rule: a grant the
  caller holds above the destination is covered at the moved node only if the caller already held it there. What
  the moved node already received (common ancestors) is not counted. Assignments of archived roles are left out
  (the role's restore checks every node it is assigned at, check_role_grant), those of archived users counted (as
  role_nodes does). Permissions are the role's own, not filtered by the holder's boundary (as check_target_user).
  A move to an ancestor (archive of a unit without cascade: its posts go to its parent) adds nothing. The checks
  read the scope tree again inside the write, after the route's locks (lock_groups / lock_unit: the old and the new
  parent, then the moved row), so a concurrent move of an ancestor is seen as committed.
- check_an_admin_remains / check_admin_kept: run after the change is written to the session (flushed here, committed
  by the route): the caller has not taken the built-in admin role at the root from themselves (409
  cannot_remove_own_admin, check_admin_kept, answered first; whether they held it is read with holds_admin_at_root
  before the change), and an active, non-archived user still holds it (409 last_admin_required). The administrator
  is a user holding the built-in system_administrator system role and the built-in administrator role at the root
  (_admin_at_root), nothing else: removing either from the last one is refused.
- lock_administrators: the first statement of every write that may take that role from someone (an assignment
  removed, a user deactivated, archived or given another type). Such writes run one after another, so the count of
  check_an_admin_remains sees the one before: two administrators deactivating each other at once can no longer
  both succeed.

What the caller holds is their AuthContext (context.load_grants: in effect, inside their boundary); what a target
holds is every_assignment, the same rows with the archived roles and without the target's boundary.
"""
from dataclasses import replace
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple, Union

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.access_control import AccessControlService
from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import (
    AuthContext, grant_rows, grants_of, load_scope_tree, permission_name, system_grant_rows,
)
from app.backend.auth.integrity import administrators_query, effective_permissions
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import BuiltinRole, Permissions, UserType
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
    """Every node the role is assigned at, assignments of archived users included (R28); a system assignment acts at
    the root"""
    keys = await session.exec(select(RoleAssignment.scope_key).where(RoleAssignment.role_id == role_id).distinct())
    nodes = {Node.parse(key) for key in keys.all()}
    system = await session.exec(select(SystemRoleAssignment.id).where(SystemRoleAssignment.role_id == role_id).limit(1))
    if system.first() is not None:
        nodes.add(ROOT)
    return frozenset(nodes)


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


async def every_assignment(session: AsyncSession, user: User) -> Tuple[Grant, ...]:
    """Every assignment of the user as a grant holding its role's permissions as the role holds them (not filtered by
    the user's boundary or the role's level: what a type change or a boundary in code could put in effect), system
    assignments (at the root) and those of archived roles included: an archived role grants nothing now (grant_rows,
    AuthContext) but its assignments come back with the role's restore"""
    tree = grants_of((await session.exec(grant_rows(user.id, include_archived_roles=True))).all())
    system = grants_of((await session.exec(system_grant_rows(user.id, include_archived_roles=True))).all(),
                       kind="system")
    return tree + system


async def check_target_user(session: AsyncSession, auth: AuthContext, target: User,
                            permission: Union[Permissions, str] = Permissions.USER_UPDATE) -> None:
    """The target's record is visible to the caller (403 user_self_only, as GET /users/{id}), the caller's permission
    covers the target user, and the caller could grant every existing assignment of the target with its role's
    permissions, archived roles included (the caller themselves always could)"""
    AccessControlService.check_user_view(auth, target)
    name = permission_name(permission)
    missing: List[str] = []
    if not auth.covers_user(name, target):
        missing = [name]
    elif target.id != auth.user.id:
        lacking: Set[str] = set()
        for grant in await every_assignment(session, target):
            lacking |= _not_covered(auth, grant.permissions, grant.node)
        missing = sorted(lacking)
    if missing:
        logger.warning(f"User {auth.user.username} tried to change user {target.id} beyond their reach: {missing}")
        raise access_denied(AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, missing=missing)


def check_boundary_change(auth: AuthContext, grants: Iterable[Grant], old_type: UserType, new_type: UserType) -> None:
    """A type change (grants: every_assignment of the target): what the new type's boundary puts in effect of each
    assignment and the old one left out is covered by the caller at the assignment's node"""
    missing: Set[str] = set()
    for grant in grants:
        before, _ = effective_permissions(old_type, grant.kind, grant.permissions, grant.role_kind)
        after, _ = effective_permissions(new_type, grant.kind, grant.permissions, grant.role_kind)
        missing |= _not_covered(auth, after - before, grant.node)
    if missing:
        logger.warning(f"User {auth.user.username} tried a type change switching on permissions they lack: "
                       f"{sorted(missing)}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=sorted(missing))


# ------------------------------------------------------------------ the scope tree: moves and restores of nodes
async def _assignments_at(session: AsyncSession, nodes: Iterable[Node]) -> Dict[int, FrozenSet[str]]:
    """assignment id -> its role's permissions, for every tree assignment at the nodes (any user, archived users
    included) of a role that is not archived (system assignments act at the root, which a move never adds)"""
    keys = sorted({node.key for node in nodes})
    if not keys:
        return {}
    rows = (await session.exec(
        select(RoleAssignment.id, Permission.name)
        .join(Role, Role.id == RoleAssignment.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(RoleAssignment.scope_key.in_(keys), Role.is_deleted == False)  # noqa: E712
    )).all()
    held: Dict[int, Set[str]] = {}
    for assignment_id, name in rows:
        names = held.setdefault(assignment_id, set())
        if name:
            names.add(name)
    return {assignment_id: frozenset(names) for assignment_id, names in held.items()}


def _check_could_grant(auth: AuthContext, assignments: Dict[int, FrozenSet[str]], node: Node, change: str) -> None:
    """check_grant_at of every assignment at the node, one 403 grant_exceeds_own_permissions listing everything
    lacking"""
    missing: Set[str] = set()
    for permissions in assignments.values():
        missing |= _not_covered(auth, permissions, node)
    if missing:
        logger.warning(f"User {auth.user.username} tried a {change} of {node.key} putting in effect "
                       f"{len(assignments)} assignments with permissions they lack there: {sorted(missing)}")
        raise access_denied(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, missing=sorted(missing))


async def _committed(session: AsyncSession, auth: AuthContext) -> AuthContext:
    """The caller with the scope tree read inside the write (after the route's locks)"""
    return replace(auth, tree=await load_scope_tree(session))


async def check_move(session: AsyncSession, auth: AuthContext, moved: Node, new_parent: Node) -> None:
    """A move of the post or group moved under new_parent (a group, the root): every assignment at the new
    parent's ancestors (itself included) that is not already above the moved node, nor inside it, nor at an
    archived node, applies at the moved subtree after the move; the caller could grant each of them at the moved
    node, as it stands before the move"""
    current = await _committed(session, auth)
    tree = current.tree
    above = set(tree.ancestors(moved)[1:])
    added = [node for node in tree.ancestors(new_parent)
             if node not in above and not tree.contains(moved, node) and not tree.is_archived(node)]
    _check_could_grant(current, await _assignments_at(session, added), moved, "move")


async def check_node_restore(session: AsyncSession, auth: AuthContext, node: Node) -> None:
    """A restore of the archived post or group: every assignment at it applies again; the caller could grant each
    of them there (a grant at the archived node itself, the caller's own included, covers nothing)"""
    current = await _committed(session, auth)
    _check_could_grant(current, await _assignments_at(session, [node]), node, "restore")


def _admin_at_root():
    """The administrators: holders of system_administrator and of administrator at the root
    (integrity.administrators_query, the one definition)"""
    return administrators_query()


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
