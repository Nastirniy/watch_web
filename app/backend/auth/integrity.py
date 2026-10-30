"""The rules of the authorization model checked in one place (authorization v3).

A grant puts in effect only the permissions of its role that have the level of the role's kind and that the
boundary of the user's type allows for that kind (auth/user_types.py: the tree boundary for tree grants, the system
boundary for system grants). This holds whatever wrote the rows: the API refuses such writes with its 409s, and
evaluation (auth/context.py) keeps what a bug, a migration or seeding could still write from ever acting.

authorization_report reads every assignment of both tables and lists the grants that put less in effect than their
roles hold, with the number of effective administrators: the start logs it (bootstrap/authorization.py), /health
shows the counts. Nothing here reaches FastAPI.

The effective administrator (administrators_query, the one definition): an active, not archived user holding the
built-in system_administrator system role and the built-in administrator role at the root, whose user type puts every
permission of both in effect, and, where login requires a client certificate (login_requires_client_certificate),
holding an active certificate: someone who can log in and act as the administrator. The last-administrator guard
(auth/privileges.py, services/certificates.revoke_certificate) and /health count these.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple, Union

from sqlalchemy import exists, func
from sqlalchemy.orm import aliased
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.user_types import USER_TYPES, boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.config import settings
from app.backend.enums.general import AppEnvironment, BuiltinRole, CertificateState, RoleKind, UserType
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import state_clause
from app.backend.utils.row_locks import lock_first

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
    system_administrators: int  # the effective administrators (administrators_query)


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


def administrator_types() -> Tuple[str, ...]:
    """The user types that put every permission of both built-in administrator roles in effect (effective_permissions
    leaves nothing out). The permissions are those of auth/builtin_roles.py: the start refuses a stored built-in role
    out of step with them (bootstrap/authorization.py)."""
    specs = (BUILTIN_ROLES[BuiltinRole.SYSTEM_ADMINISTRATOR], BUILTIN_ROLES[BuiltinRole.ADMINISTRATOR])
    return tuple(user_type.value for user_type in USER_TYPES
                 if not any(effective_permissions(user_type, spec.kind, spec.permissions, spec.kind)[1]
                            for spec in specs))


def login_requires_client_certificate() -> bool:
    """The rule of POST /auth/login: every environment but development takes the client certificate nginx verified
    (development takes the certificate values from the body, without a TLS client certificate)"""
    return settings.app.environment != AppEnvironment.DEVELOPMENT.value


def administrators_query():
    """User ids of the effective administrators (the module docstring), the one definition: privileges.py, the revoke
    of a certificate and the report read it. An administrator whose only certificate expires stops counting by
    itself: nothing is written, the state of a certificate is computed when read."""
    tree_role = aliased(Role)
    at_root = (
        select(RoleAssignment.id).join(tree_role, tree_role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == SystemRoleAssignment.user_id,
               tree_role.builtin_key == BuiltinRole.ADMINISTRATOR.value, tree_role.is_deleted == False,  # noqa: E712
               RoleAssignment.scope_key == ROOT_KEY)
    )
    conditions = [
        Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value, Role.is_deleted == False,  # noqa: E712
        exists(at_root),
        User.is_active == True, User.is_deleted == False,  # noqa: E712
        User.user_type.in_(administrator_types()),
    ]
    if login_requires_client_certificate():
        conditions.append(exists(select(UserCertificate.id).where(
            UserCertificate.user_id == SystemRoleAssignment.user_id, state_clause(CertificateState.ACTIVE))))
    return (
        select(SystemRoleAssignment.user_id)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .join(User, User.id == SystemRoleAssignment.user_id)
        .where(*conditions)
    )


async def lock_administrators(session: AsyncSession) -> None:
    """The first statement of a write that may take an effective administrator away: the row of the built-in
    system_administrator role locked (utils/row_locks.py), so such writes run one after another and each counts what
    the one before left. Later locks of the write (the user) use row_locks.lock."""
    await lock_first(session, select(Role).where(Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value))


async def is_administrator(session: AsyncSession, user_id: int) -> bool:
    """The user is an effective administrator (as written to the session: flush first when it changed)"""
    return (await session.exec(
        administrators_query().where(SystemRoleAssignment.user_id == user_id).limit(1))).first() is not None


async def an_administrator_exists(session: AsyncSession) -> bool:
    """Some user is an effective administrator (as written to the session: flush first when it changed)"""
    return (await session.exec(administrators_query().limit(1))).first() is not None


async def authorization_report(session: AsyncSession) -> AuthorizationReport:
    """Every grant of both tables (archived roles and users included: they apply again after a restore) that leaves
    permissions out, and the number of effective administrators. Three reads."""
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
    administrators = (await session.exec(select(func.count(func.distinct(holders.c.user_id))))).one()
    return AuthorizationReport(tuple(violations), int(administrators))
