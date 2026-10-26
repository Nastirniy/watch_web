"""Reads the v2 authorization data for the frozen v3 conversion (migrations/support/authz_v3.py): migration 030 and
the authz_report command use it on a database at 028 or 029. Core SQL only, reads nothing else; a missing table
counts as empty (a fresh install before seeding has no roles)."""
from typing import Dict, List, Tuple

import sqlalchemy as sa

from app.backend.migrations.support.authz_v3 import V2Assignment, V2Role, V2User


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def load_v2(conn) -> Tuple[Dict[int, V2Role], List[V2Assignment], List[V2User], List[str]]:
    """(roles by id with their permission names, role assignments, users with their v2 type, every role name)"""
    tables = _tables(conn)
    if not {"roles", "role_permissions", "permissions", "role_assignments", "users"} <= tables:
        return {}, [], [], []
    names: Dict[int, set] = {}
    for role_id, name in conn.execute(sa.text(
            "SELECT rp.role_id, p.name FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id")):
        names.setdefault(role_id, set()).add(name)
    roles = {
        role_id: V2Role(role_id, name, display_name, bool(is_deleted), builtin_key, frozenset(names.get(role_id, ())))
        for role_id, name, display_name, is_deleted, builtin_key in conn.execute(sa.text(
            "SELECT id, name, display_name, is_deleted, builtin_key FROM roles ORDER BY id"))
    }
    assignments = [V2Assignment(aid, uid, rid, key) for aid, uid, rid, key in conn.execute(sa.text(
        "SELECT id, user_id, role_id, scope_key FROM role_assignments ORDER BY id"))]
    users = [V2User(uid, user_type) for uid, user_type in conn.execute(sa.text(
        "SELECT id, user_type FROM users ORDER BY id"))]
    return roles, assignments, users, [role.name for role in roles.values()]
