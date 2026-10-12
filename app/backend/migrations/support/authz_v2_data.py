"""Authorization v2 conversion, database side: reads the legacy tables of a database at revision 022_builtin_roles
and renders the plan of authz_v2.plan_conversion for people.

FROZEN with authz_v2.py once migration 023_authorization_v2 has shipped: 023 writes describe_user() into its audit
rows and the authz_report command prints the same, so the dry run before the upgrade and the record of the upgrade
always agree. SQLAlchemy Core and the standard library only, never application code (the models change, these tables
are the 022 schema).
"""
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Set, Tuple

import sqlalchemy as sa

from app.backend.migrations.support.authz_v2 import (
    ConversionPlan, LegacyRole, LegacyUser, UserPlan, plan_conversion,
)

ADMIN_KEY = "admin"
USER_ROLES = "user_roles"
POST_ACCESS = "user_post_access"

ROLES = sa.table(
    "roles", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("display_name", sa.String),
    sa.column("is_deleted", sa.Boolean), sa.column("builtin_key", sa.String),
)
PERMISSIONS = sa.table("permissions", sa.column("id", sa.Integer), sa.column("name", sa.String))
ROLE_PERMISSIONS = sa.table(
    "role_permissions", sa.column("id", sa.Integer), sa.column("role_id", sa.Integer),
    sa.column("permission_id", sa.Integer),
)
USERS = sa.table("users", sa.column("id", sa.Integer), sa.column("username", sa.String), sa.column("post_id", sa.Integer))
USER_ROLE_ROWS = sa.table(USER_ROLES, sa.column("user_id", sa.Integer), sa.column("role_id", sa.Integer))
ACCESS_ROWS = sa.table(
    POST_ACCESS, sa.column("user_id", sa.Integer), sa.column("post_id", sa.Integer), sa.column("can_write", sa.Boolean),
)


@dataclass(frozen=True)
class LegacyData:
    """Everything plan_conversion needs, read from the legacy tables"""
    users: Tuple[LegacyUser, ...]
    roles: Mapping[int, LegacyRole]
    admin_role_id: Optional[int]   # the built-in admin role (roles.builtin_key = 'admin')
    role_names: Tuple[str, ...]    # every role name: derived roles get names none of them has

    def plan(self) -> ConversionPlan:
        return plan_conversion(self.users, self.roles, self.admin_role_id, self.role_names)


def load_legacy(conn) -> LegacyData:
    """Reads roles (archived ones included) with their permissions, users with their home post, user_roles and
    user_post_access. A table that is missing (a fresh install never had user_roles) counts as empty."""
    tables = set(sa.inspect(conn).get_table_names())
    permission_names = {pid: name for pid, name in conn.execute(sa.select(PERMISSIONS.c.id, PERMISSIONS.c.name))}
    held: Dict[int, Set[str]] = {}
    for role_id, permission_id in conn.execute(sa.select(ROLE_PERMISSIONS.c.role_id, ROLE_PERMISSIONS.c.permission_id)):
        if permission_id in permission_names:
            held.setdefault(role_id, set()).add(permission_names[permission_id])

    roles: Dict[int, LegacyRole] = {}
    admin_role_id: Optional[int] = None
    for role_id, name, display_name, is_deleted, builtin_key in conn.execute(
            sa.select(ROLES.c.id, ROLES.c.name, ROLES.c.display_name, ROLES.c.is_deleted, ROLES.c.builtin_key)):
        roles[role_id] = LegacyRole(id=role_id, name=name, display_name=display_name or name,
                                    is_deleted=bool(is_deleted), permissions=frozenset(held.get(role_id, ())))
        if builtin_key == ADMIN_KEY:
            admin_role_id = role_id

    role_ids: Dict[int, List[int]] = {}
    if USER_ROLES in tables:
        for user_id, role_id in conn.execute(sa.select(USER_ROLE_ROWS.c.user_id, USER_ROLE_ROWS.c.role_id)):
            role_ids.setdefault(user_id, []).append(role_id)
    access: Dict[int, List[Tuple[int, bool]]] = {}
    if POST_ACCESS in tables:
        for user_id, post_id, can_write in conn.execute(
                sa.select(ACCESS_ROWS.c.user_id, ACCESS_ROWS.c.post_id, ACCESS_ROWS.c.can_write)):
            access.setdefault(user_id, []).append((post_id, bool(can_write)))

    users = tuple(
        LegacyUser(id=user_id, username=username, post_id=post_id,
                   role_ids=tuple(sorted(set(role_ids.get(user_id, ())))),
                   access=tuple(sorted(access.get(user_id, ()))))
        for user_id, username, post_id in conn.execute(
            sa.select(USERS.c.id, USERS.c.username, USERS.c.post_id).order_by(USERS.c.id))
    )
    return LegacyData(users=users, roles=roles, admin_role_id=admin_role_id,
                      role_names=tuple(sorted(role.name for role in roles.values())))


def describe_user(user: LegacyUser, plan: UserPlan, roles: Mapping[int, LegacyRole]) -> dict:
    """One user of the plan: the audit row of migration 023 and an entry of the report (JSON types only)"""
    def role_name(ref) -> str:
        return roles[ref].name if isinstance(ref, int) else ref

    return {
        "user_id": user.id,
        "user": user.username,
        "type": plan.user_type,
        "before": {
            "roles": [{"id": role_id, "name": roles[role_id].name, "archived": roles[role_id].is_deleted}
                      for role_id in sorted(set(user.role_ids)) if role_id in roles],
            "post_id": user.post_id,
            "access": [{"post_id": post_id, "can_write": bool(can_write)} for post_id, can_write in sorted(user.access)],
        },
        "after": [{"role": role_name(a.role), "derived": not isinstance(a.role, int), "scope": a.node}
                  for a in plan.assignments],
        "dropped": sorted(plan.dropped),
        "differences": list(plan.differences),
    }


def describe_plan(data: LegacyData, plan: ConversionPlan) -> dict:
    """The whole plan: every user (describe_user) and the derived roles"""
    users = {user.id: user for user in data.users}
    return {
        "users": [describe_user(users[user_plan.user_id], user_plan, data.roles) for user_plan in plan.users],
        "derived_roles": [
            {"name": role.name, "display_name": role.display_name, "source_role_id": role.source_role_id,
             "permissions": sorted(role.permissions), "archived": role.is_deleted}
            for role in plan.derived_roles
        ],
    }
