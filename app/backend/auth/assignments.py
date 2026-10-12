"""Role assignments through the API (spec section 6: rules 1, 2, 5, 6 and 7).

create_assignment and delete_assignment check who may add or remove an assignment and change the row in the session;
the route flushes and commits (the unique index uq_role_assignments_user_role_scope answers assignment_exists when
two requests pass the duplicate check together) and writes the audit row. The order tells an outsider nothing about
ids: the target user and the caller's scope answer before the existence or the state of a role, a group or a post.

    create: check_target_user(user.update) 403 -> archived user 404 -> role unknown or archived 404
            -> check_grant_at(the role's permissions, the node) 403 (an unknown or archived node outside the caller's
               scope is not covered: the same 403 as a live one) -> the node exists and is live 404
            -> a role with a system permission only at the root 409 -> the ceiling of the user's type 409
            -> the same role at the same node 409
    delete: check_target_user(user.update) 403 -> archived user 404 -> the assignment of that user 404
            -> check_grant_at(its role's permissions, its node) 403 -> the user's last assignment 409
            -> the built-in admin role at the root: someone keeps it, and not taken from oneself 409

assignment_views reads the assignments of a user with the role and the scope named, as dataclasses (the route
converts them into its response model).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import delete as sa_delete, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext
from app.backend.auth.privileges import check_admin_kept, check_grant_at, check_target_user, role_permission_names
from app.backend.auth.scope import Node, NodeKind, ROOT
from app.backend.auth.user_types import excess_permissions
from app.backend.bootstrap.permissions import permission_level
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import PermissionLevel, Permissions, UserType
from app.backend.errors.problems import ApiError
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User


@dataclass(frozen=True)
class AssignmentRoleView:
    id: int
    name: str
    display_name: str
    is_builtin: bool


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


def _check_ceiling(target: User, permissions: Iterable[str]) -> None:
    """Every role of a user fits the ceiling of the user's type"""
    user_type = UserType(target.user_type)
    excess = sorted(excess_permissions(user_type, permissions))
    if excess:
        raise ApiError(ErrorCode.ROLE_EXCEEDS_USER_TYPE, user_type=user_type.value, excess_permissions=excess)


# ------------------------------------------------------------------ changes
async def create_assignment(session: AsyncSession, auth: AuthContext, target: User, role_id: int,
                            node: Node) -> RoleAssignment:
    """The assignment of the role to the target at the node, checked and added to the session (not flushed)"""
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    _refuse_archived(target)
    role = await _live_role(session, role_id)
    permissions = await role_permission_names(session, [role.id])
    check_grant_at(auth, permissions, node)
    await _check_node_live(session, node)
    _check_level(permissions, node)
    _check_ceiling(target, permissions)
    duplicate = (await session.exec(
        select(RoleAssignment.id).where(
            RoleAssignment.user_id == target.id, RoleAssignment.role_id == role.id, RoleAssignment.scope_key == node.key,
        )
    )).first()
    if duplicate is not None:
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
    check_grant_at(auth, await role_permission_names(session, [assignment.role_id]), assignment.node)

    count = (await session.exec(
        select(func.count()).select_from(RoleAssignment).where(RoleAssignment.user_id == target.id)
    )).one()
    if count <= 1:
        raise ApiError(ErrorCode.LAST_ASSIGNMENT_REQUIRED)

    builtin_key = (await session.exec(select(Role.builtin_key).where(Role.id == assignment.role_id))).one()
    removed = assignment.model_dump()
    session.expunge(assignment)
    # A core statement: no ORM cascade or relationship bookkeeping in the async session
    await session.execute(sa_delete(RoleAssignment).where(RoleAssignment.id == assignment_id))
    if builtin_key == UserType.ADMIN.value and removed["scope_key"] == ROOT.key:
        await check_admin_kept(session, auth)
    logger.info(f"User {auth.user.username} removes assignment {assignment_id} ({removed['scope_key']}) of user {target.id}")
    return removed


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
                                    is_builtin=role.builtin_key is not None),
            scope=AssignmentScopeView(kind=node.kind, id=node.id, name=name),
            created_at=assignment.created_at,
        ))
    return views
