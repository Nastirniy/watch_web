"""System role assignments through the API (authorization v3): a system role held by a user, without a node.

create_system_assignment and delete_system_assignment check who may add or remove one and change the row in the
session; the route flushes and commits (the unique index uq_system_role_assignments_user_role answers
assignment_exists when two requests pass the duplicate check together) and writes the audit row. A system role acts
at the root, so it is granted as an assignment at the root: user.update and every permission of the role held there.

    create: check_target_user(user.view 403 user_self_only, user.update) 403 -> archived user 404
            -> role unknown or archived 404
            -> a tree role 409 role_is_tree -> check_assign_at(user.update and the role's permissions, the root) 403
            -> the boundary of the user's type for system grants 409 role_exceeds_user_type
            -> the same role 409 assignment_exists
    delete: check_target_user(user.view 403 user_self_only, user.update) 403 -> archived user 404
            -> the assignment of that user 404
            -> check_assign_at(user.update and its role's permissions, the root) 403
            -> the user's last assignment of either table 409 last_assignment_required
            -> the built-in system_administrator: not taken from oneself, and someone keeps it 409

Both routes take lock_administrators first (the built-in system_administrator role row, then the user: one order
for every write that may touch the administrator); create then locks the role row (shared) before any plain read, so
a concurrent change of the role's permissions (which locks the role row) runs before or after, never in between
(v2 open item R40).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List

from loguru import logger
from sqlalchemy import delete as sa_delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.assignments import (
    AssignmentRoleView, _check_not_last_of_user, _lock_user, _refuse_archived, _require_live,
)
from app.backend.auth.context import AuthContext
from app.backend.auth.privileges import check_admin_kept, check_assign_at, check_target_user, holds_admin_at_root, \
    role_permission_names
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import excess_permissions
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import BuiltinRole, Permissions, RoleKind, UserType
from app.backend.errors.problems import ApiError
from app.backend.models.role import Role
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.utils.row_locks import lock


@dataclass(frozen=True)
class SystemAssignmentView:
    id: int
    role: AssignmentRoleView
    created_at: datetime


async def create_system_assignment(session: AsyncSession, auth: AuthContext, target: User,
                                   role_id: int) -> SystemRoleAssignment:
    """The system assignment of the role to the target, checked and added to the session (not flushed)"""
    await _lock_user(session, target)
    # The role row with a shared lock before any plain read (R40, see assignments.create_assignment)
    locked = await lock(session, select(Role).where(Role.id == role_id), shared=True)
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    _refuse_archived(target)
    role = _require_live(locked, role_id)
    if role.kind != RoleKind.SYSTEM.value:
        raise ApiError(ErrorCode.ROLE_IS_TREE, role_id=role.id)
    permissions = await role_permission_names(session, [role.id])
    check_assign_at(auth, permissions, ROOT)
    user_type = UserType(target.user_type)
    excess = sorted(excess_permissions(user_type, permissions, RoleKind.SYSTEM))
    if excess:
        raise ApiError(ErrorCode.ROLE_EXCEEDS_USER_TYPE, user_type=user_type.value, excess_permissions=excess)
    exists = (await session.exec(select(SystemRoleAssignment.id).where(
        SystemRoleAssignment.user_id == target.id, SystemRoleAssignment.role_id == role.id))).first()
    if exists is not None:
        raise ApiError(ErrorCode.ASSIGNMENT_EXISTS)

    assignment = SystemRoleAssignment(user_id=target.id, role_id=role.id, created_by=auth.user.id)
    session.add(assignment)
    logger.info(f"User {auth.user.username} gives system role {role.name} to user {target.id}")
    return assignment


async def delete_system_assignment(session: AsyncSession, auth: AuthContext, target: User, assignment_id: int) -> dict:
    """Remove one system assignment of the target (written to the session, not committed). Returns a full copy of the
    removed row, every column, for the audit log."""
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    _refuse_archived(target)
    assignment = (await session.exec(select(SystemRoleAssignment).where(
        SystemRoleAssignment.id == assignment_id, SystemRoleAssignment.user_id == target.id))).first()
    if assignment is None:
        raise ApiError(ErrorCode.ASSIGNMENT_NOT_FOUND)
    check_assign_at(auth, await role_permission_names(session, [assignment.role_id]), ROOT)
    role = (await session.exec(select(Role).where(Role.id == assignment.role_id))).one()
    await _check_not_last_of_user(session, target, role, system_id=assignment.id)

    removed = assignment.model_dump()
    administrator = role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value
    held_before = administrator and await holds_admin_at_root(session, auth.user.id)
    session.expunge(assignment)
    # A core statement: no ORM cascade or relationship bookkeeping in the async session
    await session.execute(sa_delete(SystemRoleAssignment).where(SystemRoleAssignment.id == assignment_id))
    if administrator:
        await check_admin_kept(session, auth, held_before)
    logger.info(f"User {auth.user.username} removes system assignment {assignment_id} of user {target.id}")
    return removed


async def system_assignment_views(session: AsyncSession, user_id: int) -> List[SystemAssignmentView]:
    """Every system assignment of the user, those of archived roles included, by id"""
    rows = (await session.exec(
        select(SystemRoleAssignment, Role)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .where(SystemRoleAssignment.user_id == user_id)
        .order_by(SystemRoleAssignment.id)
    )).all()
    return [SystemAssignmentView(
        id=assignment.id,
        role=AssignmentRoleView(id=role.id, name=role.name, display_name=role.display_name,
                                is_builtin=role.builtin_key is not None, is_deleted=role.is_deleted),
        created_at=assignment.created_at,
    ) for assignment, role in rows]
