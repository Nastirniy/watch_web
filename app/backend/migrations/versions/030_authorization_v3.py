"""Authorization v3: v2 data becomes tree roles, system roles and system role assignments (spec 2026-10-26, section 7).

Upgrade, one transaction, unless the "converted" row is in authz_v3_migration_log: the frozen conversion
(migrations/support/authz_v3.py, the data read by support/authz_v3_data.py) plans every role, assignment and user;
then permissions.level is written from the frozen catalog, custom roles named like a new built-in key are renamed
<name>_custom, the built-in post_admin role becomes administrator (and gets unit management) and admin becomes the
system role system_administrator, every role gets its kind, the system permissions of a mixed role go to a new
<name>_system role, the assignments of system roles leave role_assignments (those at the root come back as system
assignments, those below the root are reported and dropped), holders of the admin role get administrator at the
root, user types are renamed (post_admin -> administrator, admin -> system_administrator), one audit row per user
and per role the conversion changes or creates (action migrate, resource_type user or role, details = what changed)
and the "converted" row last. Every change
is logged (kind, ref_id, data) for the downgrade. New rows take the creation time of their source row.

Downgrade restores 029 exactly from the log: system assignments are deleted (also those made after the upgrade),
the added administrator assignments deleted and the moved assignments re-inserted with their ids, split roles and
the built-in roles seeding created afterwards (auditor, dictionary_editor, tag_editor) deleted, grants re-inserted
or deleted, names, built-in keys, texts, kinds, levels and user types restored, the log emptied. Audit rows stay.

Revision ID: 030_authorization_v3
Revises: 029_authz_v3_schema
Create Date: 2026-10-26
"""
import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

from app.backend.migrations.support.authz_v3 import LEVELS, ROOT_KEY, SYSTEM, TREE, TREE_BOUNDARY, TYPE_MAP, plan_v3
from app.backend.migrations.support.authz_v3_data import load_v2

revision = "030_authorization_v3"
down_revision = "029_authz_v3_schema"
branch_labels = None
depends_on = None

_META = sa.MetaData()


# Creation times are read and written as the stored values (no DateTime processing): a restored row is byte for
# byte the one the upgrade removed (SQLite stores the text it was given)
STAMP = sa.String


def _table(name: str, *columns) -> sa.Table:
    """A core table with its id as the primary key: inserts report the new id (lastrowid), no RETURNING (MySQL 8)"""
    return sa.Table(name, _META, sa.Column("id", sa.Integer, primary_key=True), *columns)


LOG = _table("authz_v3_migration_log", sa.Column("kind", sa.String), sa.Column("ref_id", sa.Integer),
             sa.Column("data", sa.JSON))
ROLES = _table("roles", sa.Column("name", sa.String), sa.Column("display_name", sa.String),
               sa.Column("description", sa.String), sa.Column("created_at", STAMP),
               sa.Column("updated_at", STAMP), sa.Column("is_deleted", sa.Boolean),
               sa.Column("builtin_key", sa.String), sa.Column("kind", sa.String))
GRANTS = _table("role_permissions", sa.Column("role_id", sa.Integer), sa.Column("permission_id", sa.Integer))
PERMISSIONS = _table("permissions", sa.Column("name", sa.String), sa.Column("level", sa.String))
ASSIGNMENTS = _table("role_assignments", sa.Column("user_id", sa.Integer), sa.Column("role_id", sa.Integer),
                     sa.Column("scope_group_id", sa.Integer), sa.Column("scope_post_id", sa.Integer),
                     sa.Column("scope_key", sa.String), sa.Column("created_at", STAMP),
                     sa.Column("created_by", sa.Integer))
SYSTEM_ASSIGNMENTS = _table("system_role_assignments", sa.Column("user_id", sa.Integer),
                            sa.Column("role_id", sa.Integer), sa.Column("created_at", STAMP),
                            sa.Column("created_by", sa.Integer))
USERS = _table("users", sa.Column("user_type", sa.String))
AUDIT = sa.table(
    "audit_logs", sa.column("user_id", sa.Integer), sa.column("action", sa.String),
    sa.column("resource_type", sa.String), sa.column("resource_id", sa.Integer), sa.column("details", sa.JSON),
    sa.column("success", sa.Boolean), sa.column("created_at", sa.DateTime),
)
SEEDED_LATER = ("auditor", "dictionary_editor", "tag_editor")   # built-in roles seeding creates after the upgrade


def _log(message: str) -> None:
    print(f"[030_authorization_v3] {message}")


def _record(conn, kind: str, ref_id, data=None) -> None:
    conn.execute(LOG.insert().values(kind=kind, ref_id=ref_id, data=data))


def _row(conn, table, row_id: int) -> dict:
    return dict(conn.execute(sa.select(table).where(table.c.id == row_id)).mappings().one())


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _json(row: dict) -> dict:
    return {key: (value.isoformat(sep=" ") if isinstance(value, datetime) else value) for key, value in row.items()}


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.select(LOG.c.id).where(LOG.c.kind == "converted")).first() is not None:
        _log("already converted")
        return
    roles, assignments, users, taken = load_v2(conn)
    plan = plan_v3(roles, assignments, users, taken)
    permission_ids = {name: pid for pid, name in conn.execute(sa.select(PERMISSIONS.c.id, PERMISSIONS.c.name))}
    changes = {user.id: {"user_type": [user.user_type, plan.user_types[user.id]], "moved_assignments": [],
                         "system_assignments": [], "added_assignments": [], "dropped": []} for user in users}
    role_changes = {}   # role id -> what changed (only what did)

    def changed(role_id: int, field: str, value) -> None:
        role_changes.setdefault(role_id, {})[field] = value

    for pid, name, level in conn.execute(sa.select(PERMISSIONS.c.id, PERMISSIONS.c.name, PERMISSIONS.c.level)):
        wanted = LEVELS.get(name)
        if wanted is not None and wanted != level:
            _record(conn, "level", pid, {"old": level})
            conn.execute(PERMISSIONS.update().where(PERMISSIONS.c.id == pid).values(level=wanted))

    # Custom roles named like a new built-in key first: the built-in renames take those names (roles.name is unique)
    for role_id, name in plan.role_renames.items():
        _record(conn, "role_name", role_id, {"old": roles[role_id].name})
        conn.execute(ROLES.update().where(ROLES.c.id == role_id).values(name=name))
        changed(role_id, "name", [roles[role_id].name, name])
    for role_id, key in plan.builtin_keys.items():
        old = _row(conn, ROLES, role_id)
        _record(conn, "builtin_key", role_id, {"old": old["builtin_key"], "old_name": old["name"],
                                               "old_display_name": old["display_name"],
                                               "old_description": old["description"]})
        conn.execute(ROLES.update().where(ROLES.c.id == role_id).values(builtin_key=key, name=key))
        if old["name"] != key:
            changed(role_id, "name", [old["name"], key])
        if old["builtin_key"] != key:
            changed(role_id, "builtin_key", [old["builtin_key"], key])
    for role_id, kind in plan.role_kinds.items():
        if kind != TREE:
            _record(conn, "role_kind", role_id, {"old": TREE})
            conn.execute(ROLES.update().where(ROLES.c.id == role_id).values(kind=kind))
            changed(role_id, "kind", [TREE, kind])

    for role_id, names in plan.role_permissions_removed.items():
        ids = [permission_ids[name] for name in sorted(names) if name in permission_ids]
        removed = []
        for grant in conn.execute(sa.select(GRANTS).where(GRANTS.c.role_id == role_id,
                                                          GRANTS.c.permission_id.in_(ids))).mappings().all():
            _record(conn, "removed_grant", grant["id"], dict(grant))
            conn.execute(GRANTS.delete().where(GRANTS.c.id == grant["id"]))
            removed.append(next(name for name, pid in permission_ids.items() if pid == grant["permission_id"]))
        if removed:
            changed(role_id, "permissions_removed", sorted(removed))
    for role_id, names in plan.role_permissions_added.items():
        for name in sorted(names):
            if name in permission_ids:
                new_id = conn.execute(GRANTS.insert().values(role_id=role_id, permission_id=permission_ids[name])
                                      ).inserted_primary_key[0]
                _record(conn, "added_grant", new_id)
                role_changes.setdefault(role_id, {}).setdefault("permissions_added", []).append(name)

    split_ids = {}
    for split in plan.split_roles:
        source = _row(conn, ROLES, split.source_id)
        new_id = conn.execute(ROLES.insert().values(
            name=split.name, display_name=split.display_name, description=None, created_at=source["created_at"],
            updated_at=source["created_at"], is_deleted=split.is_deleted, builtin_key=None, kind=SYSTEM,
        )).inserted_primary_key[0]
        split_ids[split.name] = new_id
        _record(conn, "split_role", new_id, {"source_id": split.source_id})
        for name in sorted(split.permissions):
            conn.execute(GRANTS.insert().values(role_id=new_id, permission_id=permission_ids[name]))
        changed(split.source_id, "split_into", new_id)
        role_changes[new_id] = {"split_from": split.source_id, "permissions": sorted(split.permissions)}

    moved = {assignment_id: _row(conn, ASSIGNMENTS, assignment_id) for assignment_id in plan.moved_assignment_ids}
    for planned in plan.system_assignments:
        role_id = split_ids[planned.role] if isinstance(planned.role, str) else planned.role
        source = moved.get(planned.source_assignment_id) or _row(conn, ASSIGNMENTS, planned.source_assignment_id)
        new_id = conn.execute(SYSTEM_ASSIGNMENTS.insert().values(
            user_id=planned.user_id, role_id=role_id, created_at=source["created_at"], created_by=None,
        )).inserted_primary_key[0]
        _record(conn, "system_assignment", new_id)
        changes[planned.user_id]["system_assignments"].append(role_id)
    for assignment_id, row in moved.items():
        _record(conn, "moved_assignment", assignment_id, _json(row))
        conn.execute(ASSIGNMENTS.delete().where(ASSIGNMENTS.c.id == assignment_id))
        changes[row["user_id"]]["moved_assignments"].append(assignment_id)
    administrator_id = plan.administrator_role_id
    if plan.added_root_administrators and administrator_id is None:
        # Upgraded past 022 without seeding in between: no built-in post_admin role to become administrator
        administrator_id = conn.execute(ROLES.insert().values(
            name="administrator", display_name="administrator", description=None, created_at=_now(), updated_at=_now(),
            is_deleted=False, builtin_key="administrator", kind=TREE,
        )).inserted_primary_key[0]
        _record(conn, "created_role", administrator_id)
        granted = [name for name in sorted(TREE_BOUNDARY["administrator"]) if name in permission_ids]
        for name in granted:
            conn.execute(GRANTS.insert().values(role_id=administrator_id, permission_id=permission_ids[name]))
        role_changes[administrator_id] = {"created": "administrator", "permissions": granted}
    for user_id in plan.added_root_administrators:
        # the holder's admin assignment at the root (moved to the system table above) gives its creation time
        source = next(row for row in moved.values() if row["user_id"] == user_id and row["scope_key"] == ROOT_KEY)
        new_id = conn.execute(ASSIGNMENTS.insert().values(
            user_id=user_id, role_id=administrator_id, scope_group_id=None, scope_post_id=None,
            scope_key=ROOT_KEY, created_at=source["created_at"], created_by=None,
        )).inserted_primary_key[0]
        _record(conn, "added_assignment", new_id)
        changes[user_id]["added_assignments"].append(new_id)
    for dropped in plan.dropped:
        owner = next((a.user_id for a in assignments if a.id == dropped.assignment_id), None)
        if owner is not None:
            changes[owner]["dropped"].append({"assignment_id": dropped.assignment_id, "reason": dropped.reason,
                                              "permissions": sorted(dropped.permissions)})

    for user in users:
        new_type = plan.user_types[user.id]
        if new_type != user.user_type:
            _record(conn, "user_type", user.id, {"old": user.user_type})
            conn.execute(USERS.update().where(USERS.c.id == user.id).values(user_type=new_type))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for user_id, details in changes.items():
        if details["user_type"][0] == details["user_type"][1] and not any(
                details[field] for field in ("moved_assignments", "system_assignments", "added_assignments", "dropped")):
            continue    # nothing changed for the user
        conn.execute(AUDIT.insert().values(user_id=None, action="migrate", resource_type="user", resource_id=user_id,
                                           details={"authorization_v3": details}, success=True, created_at=now))
    for role_id, details in sorted(role_changes.items()):
        conn.execute(AUDIT.insert().values(user_id=None, action="migrate", resource_type="role", resource_id=role_id,
                                           details={"authorization_v3": details}, success=True, created_at=now))
    _record(conn, "converted", None, {"users": len(users), "split_roles": len(plan.split_roles),
                                      "system_assignments": len(plan.system_assignments),
                                      "dropped": len(plan.dropped)})
    _log(f"converted {len(users)} users, {len(plan.split_roles)} split roles, "
         f"{len(plan.system_assignments)} system assignments, {len(plan.dropped)} grants dropped")


def downgrade() -> None:
    conn = op.get_bind()
    entries = [(kind, ref_id, json.loads(data) if isinstance(data, str) else data)
               for kind, ref_id, data in conn.execute(
                   sa.select(LOG.c.kind, LOG.c.ref_id, LOG.c.data).order_by(LOG.c.id.desc()))]
    if not any(kind == "converted" for kind, _, _ in entries):
        _log("not converted: nothing to restore")
        return
    of = lambda wanted: [(ref_id, data) for kind, ref_id, data in entries if kind == wanted]  # noqa: E731

    # Holders of system_administrator the upgrade did not make (users created or retyped after it): they get the v2
    # admin role at the root back below, their v3 types are mapped back (the v2 code knows no other type)
    logged_system = {ref_id for ref_id, _ in of("system_assignment")}
    admin_role = conn.execute(sa.select(ROLES.c.id).where(ROLES.c.builtin_key == "system_administrator")).scalar()
    later_admins = sorted({user_id for assignment_id, user_id in conn.execute(
        sa.select(SYSTEM_ASSIGNMENTS.c.id, SYSTEM_ASSIGNMENTS.c.user_id)
        .where(SYSTEM_ASSIGNMENTS.c.role_id == admin_role)) if assignment_id not in logged_system})
    conn.execute(SYSTEM_ASSIGNMENTS.delete())
    for ref_id, _ in of("added_assignment"):
        conn.execute(ASSIGNMENTS.delete().where(ASSIGNMENTS.c.id == ref_id))
    for ref_id, data in of("moved_assignment"):
        conn.execute(ASSIGNMENTS.insert().values(**data))
    split = [ref_id for ref_id, _ in of("split_role")] + [ref_id for ref_id, _ in of("created_role")]
    seeded = [rid for (rid,) in conn.execute(sa.select(ROLES.c.id).where(ROLES.c.builtin_key.in_(SEEDED_LATER)))]
    for role_id in split + seeded:
        conn.execute(ASSIGNMENTS.delete().where(ASSIGNMENTS.c.role_id == role_id))
        conn.execute(GRANTS.delete().where(GRANTS.c.role_id == role_id))
        conn.execute(ROLES.delete().where(ROLES.c.id == role_id))
    for ref_id, _ in of("added_grant"):
        conn.execute(GRANTS.delete().where(GRANTS.c.id == ref_id))
    for ref_id, data in of("removed_grant"):
        conn.execute(GRANTS.insert().values(**data))
    # Built-in names back first: they free the names the custom roles had (the reverse of the upgrade)
    for ref_id, data in of("builtin_key"):
        conn.execute(ROLES.update().where(ROLES.c.id == ref_id).values(
            builtin_key=data["old"], name=data["old_name"], display_name=data["old_display_name"],
            description=data["old_description"]))
    for ref_id, data in of("role_name"):
        conn.execute(ROLES.update().where(ROLES.c.id == ref_id).values(name=data["old"]))
    for user_id in later_admins if admin_role is not None else ():
        holds = conn.execute(sa.select(ASSIGNMENTS.c.id).where(
            ASSIGNMENTS.c.user_id == user_id, ASSIGNMENTS.c.role_id == admin_role,
            ASSIGNMENTS.c.scope_key == ROOT_KEY)).first()
        if holds is None:
            conn.execute(ASSIGNMENTS.insert().values(user_id=user_id, role_id=admin_role, scope_group_id=None,
                                                     scope_post_id=None, scope_key=ROOT_KEY, created_at=_now(),
                                                     created_by=None))
    conn.execute(ROLES.update().values(kind=TREE))
    for ref_id, data in of("level"):
        conn.execute(PERMISSIONS.update().where(PERMISSIONS.c.id == ref_id).values(level=data["old"]))
    for ref_id, data in of("user_type"):
        conn.execute(USERS.update().where(USERS.c.id == ref_id).values(user_type=data["old"]))
    for v2_type, v3_type in TYPE_MAP.items():
        conn.execute(USERS.update().where(USERS.c.user_type == v3_type).values(user_type=v2_type))
    conn.execute(LOG.delete())
    _log("restored the v2 data")
