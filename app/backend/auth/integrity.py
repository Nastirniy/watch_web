"""The rules of the authorization model checked in one place (authorization v3).

A grant puts in effect only the permissions of its role that have the level of the role's kind and that the
boundary of the user's type allows for that kind (auth/user_types.py: the tree boundary for tree grants, the system
boundary for system grants). This holds whatever wrote the rows: the API refuses such writes with its 409s, and
evaluation (auth/context.py) keeps what a bug, a migration or seeding could still write from ever acting.

authorization_report reads every assignment of both tables and lists the grants that put less in effect than their
roles hold, with the number of live system administrators: the start logs it (bootstrap/authorization.py), /health
shows the counts. Nothing here reaches FastAPI.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple, Union

from sqlalchemy import exists, func
from sqlalchemy.orm import aliased
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.user_types import boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.enums.general import BuiltinRole, RoleKind, UserType
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User

ROOT_KEY = "root"


@dataclass(frozen=True)
class Violation:
    """A grant that puts less in effect than its role holds (ids and permission names only)"""
    kind: str                   # tree (role_assignments) or system (system_role_assignments)
    assignment_id: int
    user_id: int
    role_id: int
    scope_key: str              # the node of a tree grant; root for a system one
    left_out: FrozenSet[str]


@dataclass(frozen=True)
class AuthorizationReport:
    violations: Tuple[Violation, ...]
    system_administrators: int  # active users holding system_administrator and administrator at the root


def other_level(kind: Union[RoleKind, str], names: Iterable[str]) -> List[str]:
    """The names a role of the kind cannot hold (another level, or unknown to the catalog), sorted"""
    kind = RoleKind(kind)
    return sorted(name for name in set(names)
                  if PERMISSION_LEVELS.get(name) is None or PERMISSION_LEVELS[name].value != kind.value)


def switchable_permissions(kind: Union[RoleKind, str], names: Iterable[str],
                           role_kind: Union[RoleKind, str, None] = None) -> FrozenSet[str]:
    """What a grant of the kind (its table) could put in effect for a user of some type: the names the catalog knows
    at the kind's level; nothing for a role whose kind (role_kind) is not the grant's. No boundary is applied:
    delegation counts what a type change could switch on (auth/privileges.py), never what no type can."""
    kind = RoleKind(kind)
    if role_kind is not None and RoleKind(role_kind) is not kind:
        return frozenset()
    return frozenset(name for name in set(names)
                     if PERMISSION_LEVELS.get(name) is not None and PERMISSION_LEVELS[name].value == kind.value)


def effective_permissions(user_type: Union[UserType, str], kind: Union[RoleKind, str],
                          names: Iterable[str], role_kind: Union[RoleKind, str, None] = None
                          ) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(kept, left out) of the permissions a grant of the kind (its table) holds for a user of the type: the
    switchable ones inside the boundary of the type. A name the catalog does not know (a row left in the database) is
    left out; a role whose kind (role_kind) is not the grant's puts nothing in effect."""
    names = frozenset(names)
    allowed = boundary(UserType(user_type), RoleKind(kind))
    kept = frozenset(name for name in switchable_permissions(kind, names, role_kind) if name in allowed)
    return kept, names - kept


def administrators_query():
    """User ids holding the built-in system_administrator system role (live) AND the built-in administrator tree role
    at the root (live): the one definition of the administrator, who can manage everything (privileges.py,
    the report)"""
    tree_role = aliased(Role)
    at_root = (
        select(RoleAssignment.id).join(tree_role, tree_role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == SystemRoleAssignment.user_id,
               tree_role.builtin_key == BuiltinRole.ADMINISTRATOR.value, tree_role.is_deleted == False,  # noqa: E712
               RoleAssignment.scope_key == ROOT_KEY)
    )
    return (
        select(SystemRoleAssignment.user_id)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .where(Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value, Role.is_deleted == False,  # noqa: E712
               exists(at_root))
    )


async def authorization_report(session: AsyncSession) -> AuthorizationReport:
    """Every grant of both tables (archived roles and users included: they apply again after a restore) that leaves
    permissions out, and the number of live system administrators. Three reads."""
    tree = (await session.exec(
        select(RoleAssignment.id, RoleAssignment.user_id, RoleAssignment.role_id, RoleAssignment.scope_key,
               User.user_type, Permission.name, Role.kind)
        .join(User, User.id == RoleAssignment.user_id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .join(RolePermission, RolePermission.role_id == RoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    )).all()
    system = (await session.exec(
        select(SystemRoleAssignment.id, SystemRoleAssignment.user_id, SystemRoleAssignment.role_id, User.user_type,
               Permission.name, Role.kind)
        .join(User, User.id == SystemRoleAssignment.user_id)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .join(RolePermission, RolePermission.role_id == SystemRoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    )).all()
    grants: Dict[Tuple[str, int], Tuple[int, int, str, str, str]] = {}
    names: Dict[Tuple[str, int], Set[str]] = {}
    for assignment_id, user_id, role_id, scope_key, user_type, name, role_kind in tree:
        grants[("tree", assignment_id)] = (user_id, role_id, scope_key, user_type, role_kind)
        names.setdefault(("tree", assignment_id), set()).add(name)
    for assignment_id, user_id, role_id, user_type, name, role_kind in system:
        grants[("system", assignment_id)] = (user_id, role_id, ROOT_KEY, user_type, role_kind)
        names.setdefault(("system", assignment_id), set()).add(name)
    violations = []
    for key in sorted(grants):
        user_id, role_id, scope_key, user_type, role_kind = grants[key]
        _, left_out = effective_permissions(user_type, key[0], names[key], role_kind)
        if left_out:
            violations.append(Violation(key[0], key[1], user_id, role_id, scope_key, left_out))
    holders = administrators_query().subquery()
    administrators = (await session.exec(
        select(func.count(func.distinct(User.id))).where(
            User.id.in_(select(holders.c.user_id)), User.is_active == True, User.is_deleted == False)  # noqa: E712
    )).one()
    return AuthorizationReport(tuple(violations), int(administrators))
