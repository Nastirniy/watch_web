"""TRANSITIONAL: the grants of a user computed from the old tables, deleted when the data switches to role
assignments (authorization v2, task 6).

The old tables (user_roles, users.post_id, user_post_access) stay the truth until then. Their meaning in the new
model is the frozen conversion that migration 023 will run (migrations/support/authz_v2.py), applied live here to
one user, so the routes can move to the AuthContext grants API (auth/context.py) while every existing behaviour
keeps working on the legacy fields.
"""
from typing import Dict, Iterable, Optional, Set, Tuple

from app.backend.auth.scope import Grant, Node
from app.backend.migrations.support.authz_v2 import LegacyRole, LegacyUser, REMOVED, plan_conversion
from app.backend.models.user import User


def legacy_grants(user: User, role_rows: Iterable, scope_rows: Iterable, admin_role_id: Optional[int]) -> Tuple[Grant, ...]:
    """role_rows: (role id, name, display name, builtin_key, permission name or None) for every permission of every
    role of the user that is not archived (context.role_permission_rows). scope_rows: (post id, can_write) of every
    user_post_access row of the user, archived posts included (whether a grant applies is the scope tree's
    business). admin_role_id: the built-in admin role when the user holds it.

    A derived role becomes a grant of its source role (id and name) with the derived display name and the planned
    permission set."""
    names: Dict[int, Tuple[str, str]] = {}
    builtin: Dict[int, bool] = {}
    permissions: Dict[int, Set[str]] = {}
    for role_id, name, display_name, builtin_key, permission_name in role_rows:
        names.setdefault(role_id, (name, display_name))
        builtin[role_id] = builtin_key is not None
        held = permissions.setdefault(role_id, set())
        if permission_name:
            held.add(permission_name)
    roles = {
        role_id: LegacyRole(id=role_id, name=name, display_name=display_name, is_deleted=False,
                            permissions=frozenset(permissions[role_id]))
        for role_id, (name, display_name) in names.items()
    }
    legacy_user = LegacyUser(
        id=user.id,
        username=user.username,
        post_id=user.post_id,
        role_ids=tuple(sorted(roles)),
        access=tuple(sorted((post_id, bool(can_write)) for post_id, can_write in scope_rows)),
    )
    plan = plan_conversion([legacy_user], roles, admin_role_id, [role.name for role in roles.values()])
    derived = {role.name: role for role in plan.derived_roles}

    grants = []
    for assignment in plan.users[0].assignments:
        node = Node.parse(assignment.node)
        if isinstance(assignment.role, int):
            role = roles[assignment.role]
            grants.append(Grant(role.id, role.name, role.display_name, builtin[role.id], node,
                                role.permissions - REMOVED))
        else:
            part = derived[assignment.role]
            source = roles[part.source_role_id]
            grants.append(Grant(source.id, source.name, part.display_name, False, node, part.permissions))
    return tuple(grants)
