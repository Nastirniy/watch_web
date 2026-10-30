"""The authorization checks at start (authorization v3), after migrations and seeding.

check_authorization_catalog stops the start on an error of the code or of a migration: a permission whose stored
level differs from PERMISSION_META's (a level is changed only by a migration, never by seeding), or a built-in role
whose kind or permissions differ from auth/builtin_roles.py after seeding, or a type whose base roles leave its
boundary. log_authorization_report logs an error of the data, fixed through the API: every grant that puts less in
effect than its role holds (ids and permission names only), and an ERROR line without an effective administrator
(auth/integrity.py).
"""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.integrity import authorization_report
from app.backend.auth.user_types import USER_TYPES, boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.utils.logging_config import get_database_logger

_logger = get_database_logger()


async def check_authorization_catalog(session: AsyncSession) -> None:
    """RuntimeError naming every difference between the stored catalog and the code"""
    problems = []
    for name, level in (await session.exec(select(Permission.name, Permission.level))).all():
        expected = PERMISSION_LEVELS.get(name)
        if expected is not None and expected.value != level:
            problems.append(f"permission {name} is stored as {level}, the code says {expected.value} "
                            f"(a level changes only through a migration)")
    for key, spec in BUILTIN_ROLES.items():
        role = (await session.exec(select(Role).where(Role.builtin_key == key.value))).first()
        if role is None:
            problems.append(f"built-in role {key.value} is missing")
            continue
        held = set((await session.exec(
            select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role.id))).all())
        if role.kind != spec.kind.value or held != spec.permissions:
            problems.append(f"built-in role {key.value} differs from the code (kind {role.kind}, "
                            f"{len(held ^ spec.permissions)} permissions differ)")
    for user_type, spec in USER_TYPES.items():
        for base in spec.base_roles:
            role = BUILTIN_ROLES[base.role]
            if not role.permissions <= boundary(user_type, role.kind):
                problems.append(f"type {user_type.value}: base role {base.role.value} leaves the boundary")
    if problems:
        raise RuntimeError("Authorization catalog out of step with the code: " + "; ".join(problems))


async def log_authorization_report(session: AsyncSession) -> None:
    """One WARNING per grant that leaves permissions out; an ERROR without an active system administrator"""
    report = await authorization_report(session)
    for violation in report.violations:
        _logger.warning(f"Grant {violation.kind}:{violation.assignment_id} of user {violation.user_id} (role "
                        f"{violation.role_id} at {violation.scope_key}): {sorted(violation.left_out)} not in effect "
                        f"(level or boundary of the user's type); fix it through the API")
    if report.system_administrators == 0:
        _logger.error("No effective administrator (an active user of type system_administrator holding both built-in "
                      "administrator roles, with an active certificate where login requires one): create one on the "
                      "server with "
                      "python -m app.backend.cli.init_user <certificate.pem> --username <name> --alias <display name> "
                      "--user-type system_administrator")
