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

from sqlalchemy import func
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
    system_administrators: int  # live, active users holding the built-in system_administrator role


def other_level(kind: Union[RoleKind, str], names: Iterable[str]) -> List[str]:
    """The names a role of the kind cannot hold (another level, or unknown to the catalog), sorted"""
    kind = RoleKind(kind)
    return sorted(name for name in set(names)
                  if PERMISSION_LEVELS.get(name) is None or PERMISSION_LEVELS[name].value != kind.value)


def effective_permissions(user_type: Union[UserType, str], kind: Union[RoleKind, str],
                          names: Iterable[str]) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(kept, left out) of the permissions a grant of the kind holds for a user of the type. A name the catalog does
    not know (a row left in the database) is left out."""
    kind = RoleKind(kind)
    allowed = boundary(UserType(user_type), kind)
    names = frozenset(names)
    kept = frozenset(name for name in names
                     if name in allowed and PERMISSION_LEVELS.get(name) is not None
                     and PERMISSION_LEVELS[name].value == kind.value)
    return kept, names - kept


async def authorization_report(session: AsyncSession) -> AuthorizationReport:
    """Every grant of both tables (archived roles and users included: they apply again after a restore) that leaves
    permissions out, and the number of live system administrators. Three reads."""
    tree = (await session.exec(
        select(RoleAssignment.id, RoleAssignment.user_id, RoleAssignment.role_id, RoleAssignment.scope_key,
               User.user_type, Permission.name)
        .join(User, User.id == RoleAssignment.user_id)
        .join(RolePermission, RolePermission.role_id == RoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    )).all()
    system = (await session.exec(
        select(SystemRoleAssignment.id, SystemRoleAssignment.user_id, SystemRoleAssignment.role_id, User.user_type,
               Permission.name)
        .join(User, User.id == SystemRoleAssignment.user_id)
        .join(RolePermission, RolePermission.role_id == SystemRoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    )).all()
    grants: Dict[Tuple[str, int], Tuple[int, int, str, str]] = {}
    names: Dict[Tuple[str, int], Set[str]] = {}
    for assignment_id, user_id, role_id, scope_key, user_type, name in tree:
        grants[("tree", assignment_id)] = (user_id, role_id, scope_key, user_type)
        names.setdefault(("tree", assignment_id), set()).add(name)
    for assignment_id, user_id, role_id, user_type, name in system:
        grants[("system", assignment_id)] = (user_id, role_id, ROOT_KEY, user_type)
        names.setdefault(("system", assignment_id), set()).add(name)
    violations = []
    for key in sorted(grants):
        user_id, role_id, scope_key, user_type = grants[key]
        _, left_out = effective_permissions(user_type, key[0], names[key])
        if left_out:
            violations.append(Violation(key[0], key[1], user_id, role_id, scope_key, left_out))
    administrators = (await session.exec(
        select(func.count(func.distinct(SystemRoleAssignment.user_id)))
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .join(User, User.id == SystemRoleAssignment.user_id)
        .where(Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value, Role.is_deleted == False,  # noqa: E712
               User.is_active == True, User.is_deleted == False)  # noqa: E712
    )).one()
    return AuthorizationReport(tuple(violations), int(administrators))
