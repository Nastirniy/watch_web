"""Role assignments through the API (spec section 6: rules 1, 2, 4, 5, 6 and 7).

create_assignment and delete_assignment check who may add or remove an assignment and change the row in the session;
the route flushes and commits (the unique index uq_role_assignments_user_role_scope answers assignment_exists when
two requests pass the duplicate check together) and writes the audit row. The order tells an outsider nothing about
ids: the target user and the caller's scope answer before the existence or the state of a role, a group or a post.

    create: check_target_user(user.update) 403 -> archived user 404 -> role unknown or archived 404
            -> check_assign_at(user.update and the role's permissions, the node) 403 (an unknown or archived node
               outside the caller's scope is not covered: the same 403 as a live one, a role without permissions
               included) -> the node exists and is live 404
            -> a role with a system permission only at the root 409 -> the ceiling of the user's type 409
            -> the same role at the same node 409
    delete: check_target_user(user.update) 403 -> archived user 404 -> the assignment of that user 404
            -> check_assign_at(user.update and its role's permissions, its node) 403
            -> the user's last assignment 409 (of a live role: another one of a live role remains; an assignment of
               an archived role grants nothing)

change_user_type (PATCH /users/{user_id} with user_type, rule 4; the route ran rule 2, check_target_user with
user.update, first) gives the user the base roles of the new type (auth/user_types.py): every assignment of the old
type's tree base role gets the new type's tree base role at the same node (at the root when the new one is at_root;
an assignment the user already has is kept and the replaced row merged into it), the system base roles of the old
type the new one lacks are removed and those it adds are given; then every assignment of both tables is checked
against the boundary of the new type:

            check_assign_at(user.update and the new tree base role's permissions, at the node of the default
               assignment of a new user (the home post; the root without one or for an at_root base role), also when
               nothing is replaced, and at each target node; each added system base role at the root) 403
            -> the replacements written (the unique index answers a concurrent assignment of the new base role at
               the same node: the route maps it to 409 assignment_exists)
            -> every assignment of both tables, those of archived roles included, within the new boundary 409
               user_type_ceiling_exceeded
            (the route then ends the user's sessions, runs check_admin_kept and writes the audit row, one transaction)

create_assignment and change_user_type lock the target's users row first (SELECT ... FOR UPDATE, _lock_user), the
role edits the roles row (get_role_or_404(lock=True)): concurrent changes of one user, or of one role, run one after
another.

check_role_permissions_added (PATCH /roles/{role_id}, PUT /roles/{role_id}/permissions/{permission_id}): permissions
added to a role reach every holder at once, so a system permission needs every assignment of the role at the root
(409 role_requires_root_scope) and every holder's type must allow the added permissions (409
role_holders_exceed_user_type). Archived users are holders too: a restore gives them the role back as it is.

assignment_views reads the assignments of a user with the role and the scope named, as dataclasses (the route
converts them into its response model).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from loguru import logger
from sqlalchemy import delete as sa_delete, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext
from app.backend.auth.privileges import (
    check_assign_at, check_target_user, holds_admin_at_root, role_permission_names,
)
from app.backend.auth.scope import Node, NodeKind, ROOT
from app.backend.auth.user_types import USER_TYPES, excess_permissions
from app.backend.bootstrap.permissions import permission_level
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, UserType
from app.backend.errors.problems import ApiError
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User


@dataclass(frozen=True)
class AssignmentRoleView:
    id: int
    name: str
    display_name: str
    is_builtin: bool
    is_deleted: bool    # an archived role grants nothing until it is restored


@dataclass(frozen=True)
class AssignmentScopeView:
    kind: NodeKind
    id: Optional[int]
    name: str           # the group or the post; the application name at the root


@dataclass(frozen=True)
class AssignmentView:
    id: int
    role: AssignmentRoleView
    scope: AssignmentScopeView
    created_at: datetime


# ------------------------------------------------------------------ checks
def _refuse_archived(target: User) -> None:
    """An archived user is changed only by its restore"""
    if target.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)


async def _live_role(session: AsyncSession, role_id: int) -> Role:
    role = (await session.exec(select(Role).where(Role.id == role_id, Role.is_deleted == False))).first()  # noqa: E712
    if role is None:
        raise ApiError(ErrorCode.ROLE_NOT_FOUND, role_id=role_id)
    return role


async def _check_node_live(session: AsyncSession, node: Node) -> None:
    """Spec section 6 rule 7: an unknown or archived group or post takes no assignment"""
    if node.kind == "group":
        found = (await session.exec(
            select(ScopeGroup.id).where(ScopeGroup.id == node.id, ScopeGroup.is_deleted == False)  # noqa: E712
        )).first()
        if found is None:
            raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    elif node.kind == "post":
        found = (await session.exec(select(Post.id).where(Post.id == node.id, Post.is_deleted == False))).first()  # noqa: E712
        if found is None:
            raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=node.id)


def _check_level(permissions: Iterable[str], node: Node) -> None:
    """A role holding a system permission is assignable only at the root"""
    if node == ROOT:
        return
    system = sorted(name for name in permissions if permission_level(name) is PermissionLevel.SYSTEM)
    if system:
        raise ApiError(ErrorCode.ROLE_REQUIRES_ROOT_SCOPE, system_permissions=system)


async def _assignment_exists(session: AsyncSession, user_id: int, role_id: int, node: Node) -> bool:
    """The user already holds the role at the node (two requests may both see False: the unique index answers then)"""
    found = (await session.exec(
        select(RoleAssignment.id).where(
            RoleAssignment.user_id == user_id, RoleAssignment.role_id == role_id, RoleAssignment.scope_key == node.key,
        )
    )).first()
    return found is not None


async def _check_not_last(session: AsyncSession, target: User, assignment: RoleAssignment, role: Role) -> None:
    """Spec section 6 rule 6: a user keeps at least one assignment. An assignment of an archived role grants nothing,
    so the last assignment of a live role stays while only such assignments would remain; an assignment of an
    archived role goes while any other assignment remains"""
    others = (
        select(func.count()).select_from(RoleAssignment)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == target.id, RoleAssignment.id != assignment.id)
    )
    if not role.is_deleted:
        others = others.where(Role.is_deleted == False)  # noqa: E712
    if (await session.exec(others)).one() == 0:
        raise ApiError(ErrorCode.LAST_ASSIGNMENT_REQUIRED)


def _check_ceiling(target: User, permissions: Iterable[str]) -> None:
    """Every role of a user fits the ceiling of the user's type"""
    user_type = UserType(target.user_type)
    excess = sorted(excess_permissions(user_type, permissions))
    if excess:
        raise ApiError(ErrorCode.ROLE_EXCEEDS_USER_TYPE, user_type=user_type.value, excess_permissions=excess)


# ------------------------------------------------------------------ changes
async def _lock_user(session: AsyncSession, target: User) -> None:
    """SELECT ... FOR UPDATE on the target's row (nothing on sqlite), the row read again (pending changes of the
    request are flushed first): a concurrent assignment or type change of the same user waits for this transaction,
    and the checks read the user's type as committed"""
    (await session.exec(
        select(User).where(User.id == target.id).with_for_update().execution_options(populate_existing=True)
    )).one()


async def create_assignment(session: AsyncSession, auth: AuthContext, target: User, role_id: int,
                            node: Node) -> RoleAssignment:
    """The assignment of the role to the target at the node, checked and added to the session (not flushed)"""
    await _lock_user(session, target)
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    _refuse_archived(target)
    role = await _live_role(session, role_id)
    permissions = await role_permission_names(session, [role.id])
    check_assign_at(auth, permissions, node)
    await _check_node_live(session, node)
    _check_level(permissions, node)
    _check_ceiling(target, permissions)
    if await _assignment_exists(session, target.id, role.id, node):
        raise ApiError(ErrorCode.ASSIGNMENT_EXISTS)

    assignment = RoleAssignment(user_id=target.id, role_id=role.id, created_by=auth.user.id, **scope_columns(node))
    session.add(assignment)
    logger.info(f"User {auth.user.username} assigns role {role.name} at {node.key} to user {target.id}")
    return assignment


async def delete_assignment(session: AsyncSession, auth: AuthContext, target: User, assignment_id: int) -> dict:
    """Remove one assignment of the target (written to the session, not committed). Returns a full copy of the
    removed row, every column, for the audit log."""
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    _refuse_archived(target)
    assignment = (await session.exec(
        select(RoleAssignment).where(RoleAssignment.id == assignment_id, RoleAssignment.user_id == target.id)
    )).first()
    if assignment is None:
        raise ApiError(ErrorCode.ASSIGNMENT_NOT_FOUND)
    check_assign_at(auth, await role_permission_names(session, [assignment.role_id]), assignment.node)
    role = (await session.exec(select(Role).where(Role.id == assignment.role_id))).one()
    await _check_not_last(session, target, assignment, role)

    removed = assignment.model_dump()
    session.expunge(assignment)
    # A core statement: no ORM cascade or relationship bookkeeping in the async session
    await session.execute(sa_delete(RoleAssignment).where(RoleAssignment.id == assignment_id))
    logger.info(f"User {auth.user.username} removes assignment {assignment_id} ({removed['scope_key']}) of user {target.id}")
    return removed


# ------------------------------------------------------------------ the type of a user (rule 4)
@dataclass(frozen=True)
class TypeChange:
    """What change_user_type did: the audit row and the route read it"""
    old_type: UserType
    new_type: UserType
    # One entry per assignment of the old tree base role, by id: "before" (every column) and either "after" (every
    # column: the same row with the new role, at the root for an at_root base role) or "merged_into" (the row was
    # removed, the user already held the new base role at that node: the id of that assignment)
    replaced: List[dict]
    # System base roles: the removed system assignments (every column) and the added ones (every column)
    system_removed: List[dict]
    system_added: List[dict]
    # The caller held the built-in system_administrator role before the change (check_admin_kept)
    admin_held_before: bool


async def _builtin_role(session: AsyncSession, key: BuiltinRole) -> Role:
    return (await session.exec(select(Role).where(Role.builtin_key == key.value))).one()


async def _permissions_by_role(session: AsyncSession, role_ids: Iterable[int]) -> Dict[int, Set[str]]:
    ids = sorted(set(role_ids))
    by_role: Dict[int, Set[str]] = {role_id: set() for role_id in ids}
    if ids:
        rows = await session.exec(
            select(RolePermission.role_id, Permission.name)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(ids))
        )
        for role_id, name in rows.all():
            by_role[role_id].add(name)
    return by_role


async def _check_type_ceiling(session: AsyncSession, target: User, user_type: UserType) -> None:
    """Every assignment of the user, system assignments included, fits the boundary of the type, those of archived
    roles included (they apply again when the role is restored)"""
    views = await assignment_views(session, target.id)
    system_rows = (await session.exec(
        select(SystemRoleAssignment.id, Role.id, Role.name, Role.display_name)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .where(SystemRoleAssignment.user_id == target.id)
        .order_by(SystemRoleAssignment.id)
    )).all()
    by_role = await _permissions_by_role(session, [*(view.role.id for view in views), *(row[1] for row in system_rows)])
    exceeding = []
    for assignment_id, role_id, name, display_name in system_rows:
        excess = sorted(excess_permissions(user_type, by_role[role_id]))
        if excess:
            exceeding.append({
                "id": assignment_id,
                "role": {"id": role_id, "name": name, "display_name": display_name},
                "scope": {"kind": "root", "id": None, "name": settings.app.name},
                "excess_permissions": excess,
            })
    for view in views:
        excess = sorted(excess_permissions(user_type, by_role[view.role.id]))
        if excess:
            exceeding.append({
                "id": view.id,
                "role": {"id": view.role.id, "name": view.role.name, "display_name": view.role.display_name},
                "scope": {"kind": view.scope.kind, "id": view.scope.id, "name": view.scope.name},
                "excess_permissions": excess,
            })
    if exceeding:
        logger.warning(f"Refused type {user_type.value} for user {target.id}: {len(exceeding)} assignments exceed it")
        raise ApiError(ErrorCode.USER_TYPE_CEILING_EXCEEDED, user_type=user_type.value, assignments=exceeding)


async def _held_nodes(session: AsyncSession, user_id: int, role_id: int) -> Dict[str, int]:
    """scope key -> id of the user's assignments of the role (a concurrent request may add one after this read: the
    unique index answers then)"""
    return dict((await session.exec(
        select(RoleAssignment.scope_key, RoleAssignment.id)
        .where(RoleAssignment.user_id == user_id, RoleAssignment.role_id == role_id)
    )).all())


async def change_user_type(session: AsyncSession, auth: AuthContext, target: User,
                           new_type: UserType) -> Optional[TypeChange]:
    """Give the target another type (written to the session and flushed, not committed). The route has run rule 2
    (check_target_user with user.update) and, after this, ends the user's sessions and runs check_admin_kept. None
    when the type stays the same: nothing changes."""
    await _lock_user(session, target)
    old_type = UserType(target.user_type)
    if new_type is old_type:
        return None
    old_spec, new_spec = USER_TYPES[old_type], USER_TYPES[new_type]
    old_base = await _builtin_role(session, old_spec.tree_base.role)
    new_base = await _builtin_role(session, new_spec.tree_base.role)
    new_permissions = await role_permission_names(session, [new_base.id])
    to_root = new_spec.tree_base.at_root

    rows = (await session.exec(
        select(RoleAssignment)
        .where(RoleAssignment.user_id == target.id, RoleAssignment.role_id == old_base.id)
        .order_by(RoleAssignment.id)
    )).all()
    plan = [(row, ROOT if to_root else row.node) for row in rows]
    # The same base role at the same node: nothing to replace
    plan = [(row, node) for row, node in plan if not (row.role_id == new_base.id and row.scope_key == node.key)]
    # The bar of a new user's default assignment, whether or not anything is replaced (R29)
    default_node = ROOT if target.post_id is None or to_root else Node.post(target.post_id)
    for node in (default_node, *(node for _, node in plan)):
        check_assign_at(auth, new_permissions, node)

    old_system = {base.role for base in old_spec.system_bases}
    new_system = {base.role for base in new_spec.system_bases}
    added_roles = [await _builtin_role(session, key) for key in sorted(new_system - old_system)]
    for role in added_roles:
        check_assign_at(auth, await role_permission_names(session, [role.id]), ROOT)
    admin_held_before = await holds_admin_at_root(session, auth.user.id)

    held = await _held_nodes(session, target.id, new_base.id)
    replaced: List[dict] = []
    for row, node in plan:
        before = row.model_dump()
        if node.key in held:
            session.expunge(row)
            # A core statement, like the assignment API: no ORM cascade in the async session
            await session.execute(sa_delete(RoleAssignment).where(RoleAssignment.id == row.id))
            replaced.append({"before": before, "merged_into": held[node.key]})
            continue
        row.role_id = new_base.id
        for column, value in scope_columns(node).items():
            setattr(row, column, value)
        session.add(row)
        held[node.key] = row.id
        replaced.append({"before": before, "after": row.model_dump()})

    system_removed: List[dict] = []
    for key in sorted(old_system - new_system):
        role = await _builtin_role(session, key)
        for row in (await session.exec(select(SystemRoleAssignment).where(
                SystemRoleAssignment.user_id == target.id, SystemRoleAssignment.role_id == role.id))).all():
            system_removed.append(row.model_dump())
            session.expunge(row)
            await session.execute(sa_delete(SystemRoleAssignment).where(SystemRoleAssignment.id == row.id))
    system_added: List[SystemRoleAssignment] = []
    for role in added_roles:
        exists = (await session.exec(select(SystemRoleAssignment.id).where(
            SystemRoleAssignment.user_id == target.id, SystemRoleAssignment.role_id == role.id))).first()
        if exists is None:
            row = SystemRoleAssignment(user_id=target.id, role_id=role.id, created_by=auth.user.id)
            session.add(row)
            system_added.append(row)

    target.user_type = new_type.value
    session.add(target)
    await session.flush()
    await _check_type_ceiling(session, target, new_type)
    logger.info(f"User {auth.user.username} changes the type of user {target.id} from {old_type.value} to "
                f"{new_type.value} ({len(replaced)} assignments of {old_base.name} replaced, "
                f"{len(system_removed)} system assignments removed, {len(system_added)} added)")
    return TypeChange(old_type, new_type, replaced, system_removed, [row.model_dump() for row in system_added],
                      admin_held_before)


# ------------------------------------------------------------------ role edits (holders of the role)
async def check_role_permissions_added(session: AsyncSession, role_id: int, added: Iterable[str]) -> None:
    """Permissions added to a role apply at once at every assignment of it, those of archived users included (the
    restore of a user gives the role back as it is): a system permission needs every assignment at the root (409
    role_requires_root_scope), and the type of every holder must allow the added permissions (409
    role_holders_exceed_user_type, every such holder listed once)."""
    added = sorted(set(added))
    if not added:
        return
    rows = (await session.exec(
        select(User.id, User.username, User.user_type, RoleAssignment.scope_key)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(RoleAssignment.role_id == role_id)
        .order_by(User.id, RoleAssignment.id)
    )).all()
    below_root = next((Node.parse(scope_key) for *_, scope_key in rows if scope_key != ROOT.key), None)
    if below_root is not None:
        _check_level(added, below_root)

    holders: Dict[int, dict] = {}
    for user_id, username, user_type, _ in rows:
        excess = sorted(excess_permissions(UserType(user_type), added))
        if excess and user_id not in holders:
            holders[user_id] = {"id": user_id, "username": username, "user_type": user_type, "excess_permissions": excess}
    if holders:
        logger.warning(f"Refused permissions {added} for role {role_id}: holders {sorted(holders)} cannot hold them")
        raise ApiError(ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE, users=list(holders.values()))


# ------------------------------------------------------------------ reads
async def assignment_views(session: AsyncSession, user_id: int,
                           assignment_id: Optional[int] = None) -> List[AssignmentView]:
    """Every assignment of the user (one with assignment_id), assignments of archived roles included, by id"""
    query = (
        select(RoleAssignment, Role)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == user_id)
        .order_by(RoleAssignment.id)
    )
    if assignment_id is not None:
        query = query.where(RoleAssignment.id == assignment_id)
    rows = (await session.exec(query)).all()

    group_ids = sorted({assignment.scope_group_id for assignment, _ in rows if assignment.scope_group_id is not None})
    post_ids = sorted({assignment.scope_post_id for assignment, _ in rows if assignment.scope_post_id is not None})
    names: Dict[str, Dict[int, str]] = {"group": {}, "post": {}}
    if group_ids:
        names["group"] = dict((await session.exec(
            select(ScopeGroup.id, ScopeGroup.name).where(ScopeGroup.id.in_(group_ids))
        )).all())
    if post_ids:
        names["post"] = dict((await session.exec(select(Post.id, Post.name).where(Post.id.in_(post_ids)))).all())

    views = []
    for assignment, role in rows:
        node = assignment.node
        name = settings.app.name if node == ROOT else names[node.kind].get(node.id, "")
        views.append(AssignmentView(
            id=assignment.id,
            role=AssignmentRoleView(id=role.id, name=role.name, display_name=role.display_name,
                                    is_builtin=role.builtin_key is not None, is_deleted=role.is_deleted),
            scope=AssignmentScopeView(kind=node.kind, id=node.id, name=name),
            created_at=assignment.created_at,
        ))
    return views
