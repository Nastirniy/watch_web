"""Authorization v2: role assignments replace user_roles and user_post_access; every user gets a user type.

Upgrade (spec section 8):
1. role_assignments (user + role + scope node; unique per user, role and scope_key; CHECK never a group and a post
   at once), users.user_type (nullable during the conversion) and authz_v2_migration_log (kind, ref_id, data: what
   the downgrade needs), each guarded with an inspector check: a fresh install built by 002_sync_schema already has
   users.user_type.
2. Unless the database is converted already (the "converted" row in the log, or user_roles_v1 / user_post_access_v1
   from a conversion before that row existed): the frozen conversion (migrations/support/authz_v2.py) plans every
   user from user_roles, users.post_id and user_post_access (support/authz_v2_data.py reads them; a missing table
   counts as empty: a fresh install has no user_roles). The derived roles are inserted with their permissions (a
   permission the plan needs and the table lacks, media.monitor before seeding added it, is inserted too and logged),
   then the assignments (only "root" and "p:<id>" nodes; anything else stops the migration), users.user_type and one
   audit row per user (action migrate, resource_type user, details = the user's plan, what authz_report prints). The
   admin.true and post.access_all rows leave permissions and role_permissions (logged with every column); the
   "converted" row is written last, in the same transaction.
   Then, converted now or before, each of user_roles and user_post_access still under its legacy name (and without
   its *_v1) is renamed to *_v1 and kept for the downgrade (a later cleanup revision drops them). MySQL commits the
   conversion with the first RENAME: a rename that failed, or a crash between the two, is finished by a rerun.
3. users.user_type becomes NOT NULL.

Downgrade restores exactly: the legacy tables come back from *_v1 (a fresh install has no user_roles_v1: user_roles
is created empty with its original schema), the removed permission rows and grants come back with their ids from the
log, the derived roles and the permissions the upgrade inserted are deleted, role_assignments, users.user_type and the
log (the "converted" row with it) are dropped. Audit rows stay (history); assignments made after the upgrade are lost
with role_assignments.

Revision ID: 023_authorization_v2
Revises: 022_builtin_roles
Create Date: 2026-10-12
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from alembic import op
import sqlalchemy as sa

from app.backend.migrations.support.authz_v2 import REMOVED
from app.backend.migrations.support.authz_v2_data import describe_user, load_legacy

revision = "023_authorization_v2"
down_revision = "022_builtin_roles"
branch_labels = None
depends_on = None

ASSIGNMENTS = "role_assignments"
LOG = "authz_v2_migration_log"
USERS = "users"
USER_TYPE = "user_type"
USER_ROLES, USER_ROLES_V1 = "user_roles", "user_roles_v1"
POST_ACCESS, POST_ACCESS_V1 = "user_post_access", "user_post_access_v1"
UNIQUE = "uq_role_assignments_user_role_scope"
CHECK = "ck_role_assignments_one_scope"
INDEXED = ("user_id", "role_id", "scope_group_id", "scope_post_id")
ROOT_NODE = "root"                               # the scope_key of the root; a post is "p:<id>"

# What the log records (kind: ref_id, data)
REMOVED_PERMISSION = "removed_permission"        # permissions.id: the row
REMOVED_GRANT = "removed_role_permission"        # role_permissions.id: the row
DERIVED_ROLE = "derived_role"                    # roles.id: name, source role
ADDED_PERMISSION = "added_permission"            # permissions.id: name
CONVERTED = "converted"                          # no ref_id: users and derived roles converted (the last row written)

# Tables the conversion inserts into and needs the new id of: a primary key column (inserted_primary_key)
_META = sa.MetaData()
ROLES = sa.Table(
    "roles", _META, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String),
    sa.Column("display_name", sa.String), sa.Column("description", sa.String), sa.Column("created_at", sa.DateTime),
    sa.Column("updated_at", sa.DateTime), sa.Column("is_deleted", sa.Boolean), sa.Column("builtin_key", sa.String),
)
PERMISSIONS = sa.Table(
    "permissions", _META, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String),
    sa.Column("display_name", sa.String), sa.Column("description", sa.String),
)
ROLE_PERMISSIONS = sa.table(
    "role_permissions", sa.column("id", sa.Integer), sa.column("role_id", sa.Integer),
    sa.column("permission_id", sa.Integer),
)
USER_TYPES = sa.table(USERS, sa.column("id", sa.Integer), sa.column(USER_TYPE, sa.String))
ASSIGNMENT_ROWS = sa.table(
    ASSIGNMENTS, sa.column("user_id", sa.Integer), sa.column("role_id", sa.Integer),
    sa.column("scope_group_id", sa.Integer), sa.column("scope_post_id", sa.Integer), sa.column("scope_key", sa.String),
    sa.column("created_at", sa.DateTime), sa.column("created_by", sa.Integer),
)
LOG_ROWS = sa.table(LOG, sa.column("id", sa.Integer), sa.column("kind", sa.String), sa.column("ref_id", sa.Integer),
                    sa.column("data", sa.JSON))
AUDIT = sa.table(
    "audit_logs", sa.column("user_id", sa.Integer), sa.column("action", sa.String),
    sa.column("resource_type", sa.String), sa.column("resource_id", sa.Integer), sa.column("details", sa.JSON),
    sa.column("success", sa.Boolean), sa.column("created_at", sa.DateTime),
)


def _log(message: str) -> None:
    print(f"[023_authorization_v2] {message}")


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _record(kind: str, ref_id: Optional[int], data: dict) -> None:
    op.get_bind().execute(LOG_ROWS.insert().values(kind=kind, ref_id=ref_id, data=data))


# ---------------------------------------------------------------------------------------------------- upgrade
def upgrade() -> None:
    tables = _tables()
    if USERS not in tables or "roles" not in tables:
        return
    _create_assignments(tables)
    _add_user_type()
    if LOG not in tables:
        _log(f"creating {LOG}")
        op.create_table(
            LOG,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
        )

    tables = _tables()
    if _converted(tables):
        _log("already converted: the converted marker or a *_v1 table is present")
    else:
        _convert()
    # Independently of the branch above: MySQL commits the conversion with the first RENAME, so a rename that failed
    # or a crash between the two leaves converted data under a legacy name; a rerun finishes the renames
    for legacy, kept in ((USER_ROLES, USER_ROLES_V1), (POST_ACCESS, POST_ACCESS_V1)):
        if legacy in tables and kept not in tables:
            _log(f"renaming {legacy} -> {kept}")
            op.rename_table(legacy, kept)

    column = next(c for c in sa.inspect(op.get_bind()).get_columns(USERS) if c["name"] == USER_TYPE)
    if column["nullable"]:
        _log(f"{USERS}.{USER_TYPE} NOT NULL")
        with op.batch_alter_table(USERS) as batch:
            batch.alter_column(USER_TYPE, existing_type=sa.String(length=32), nullable=False)


def _create_assignments(tables: set) -> None:
    if ASSIGNMENTS not in tables:
        _log(f"creating {ASSIGNMENTS}")
        op.create_table(
            ASSIGNMENTS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
            sa.Column("scope_group_id", sa.Integer(), sa.ForeignKey("scope_groups.id"), nullable=True),
            sa.Column("scope_post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=True),
            sa.Column("scope_key", sa.String(length=24), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.UniqueConstraint("user_id", "role_id", "scope_key", name=UNIQUE),
            sa.CheckConstraint("scope_group_id IS NULL OR scope_post_id IS NULL", name=CHECK),
        )
    present = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(ASSIGNMENTS)}
    for column in INDEXED:
        name = f"ix_{ASSIGNMENTS}_{column}"
        if name not in present:
            op.create_index(name, ASSIGNMENTS, [column])


def _converted(tables: set) -> bool:
    """The data is converted already: the marker _convert writes in the transaction of its rows, or a *_v1 table (a
    database converted before the marker existed)"""
    if USER_ROLES_V1 in tables or POST_ACCESS_V1 in tables:
        return True
    marker = sa.select(LOG_ROWS.c.id).where(LOG_ROWS.c.kind == CONVERTED).limit(1)
    return op.get_bind().execute(marker).first() is not None


def scope_post_id(node: str) -> Optional[int]:
    """The post id of a plan node: None for "root", the id for "p:<id>". The frozen plan emits nothing else (no
    groups); any other node stops the migration rather than becoming an assignment with both scope ids NULL"""
    if node == ROOT_NODE:
        return None
    kind, _, ident = node.partition(":")
    if kind == "p" and ident.isdigit() and ident == str(int(ident)) and int(ident) > 0:
        return int(ident)
    raise ValueError(f"unexpected scope node in the conversion plan: {node!r}")


def _add_user_type() -> None:
    if USER_TYPE not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(USERS)}:
        _log(f"adding column {USERS}.{USER_TYPE}")
        op.add_column(USERS, sa.Column(USER_TYPE, sa.String(length=32), nullable=True))


def _convert() -> None:
    bind = op.get_bind()
    data = load_legacy(bind)
    plan = data.plan()
    _log(f"converting {len(plan.users)} user(s), {len(plan.derived_roles)} derived role(s)")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    permission_ids = {name: pid for pid, name in bind.execute(sa.select(PERMISSIONS.c.id, PERMISSIONS.c.name))}

    def permission_id(name: str) -> int:
        if name not in permission_ids:
            result = bind.execute(PERMISSIONS.insert().values(name=name, display_name=name, description=""))
            permission_ids[name] = result.inserted_primary_key[0]
            _record(ADDED_PERMISSION, permission_ids[name], {"name": name})
            _log(f"inserted permission {name} (seeding sets its display texts)")
        return permission_ids[name]

    derived_ids: Dict[str, int] = {}
    for role in plan.derived_roles:
        result = bind.execute(ROLES.insert().values(
            name=role.name, display_name=role.display_name, description=None, created_at=now, updated_at=now,
            is_deleted=role.is_deleted, builtin_key=None,
        ))
        derived_ids[role.name] = result.inserted_primary_key[0]
        for name in sorted(role.permissions):
            bind.execute(ROLE_PERMISSIONS.insert().values(role_id=derived_ids[role.name], permission_id=permission_id(name)))
        _record(DERIVED_ROLE, derived_ids[role.name], {"name": role.name, "source_role_id": role.source_role_id})
        _log(f"derived role {role.name} from role {role.source_role_id}: {', '.join(sorted(role.permissions))}")

    users = {user.id: user for user in data.users}
    for user_plan in plan.users:
        for assignment in user_plan.assignments:
            post_id = scope_post_id(assignment.node)
            bind.execute(ASSIGNMENT_ROWS.insert().values(
                user_id=user_plan.user_id,
                role_id=assignment.role if isinstance(assignment.role, int) else derived_ids[assignment.role],
                scope_group_id=None, scope_post_id=post_id, scope_key=assignment.node, created_at=now, created_by=None,
            ))
        bind.execute(sa.update(USER_TYPES).where(USER_TYPES.c.id == user_plan.user_id).values(user_type=user_plan.user_type))
        bind.execute(AUDIT.insert().values(
            user_id=None, action="migrate", resource_type="user", resource_id=user_plan.user_id,
            details=describe_user(users[user_plan.user_id], user_plan, data.roles), success=True, created_at=now,
        ))

    removed = bind.execute(
        sa.select(PERMISSIONS.c.id, PERMISSIONS.c.name, PERMISSIONS.c.display_name, PERMISSIONS.c.description)
        .where(PERMISSIONS.c.name.in_(sorted(REMOVED))).order_by(PERMISSIONS.c.id)
    ).all()
    removed_ids = [row.id for row in removed]
    if removed_ids:
        grants = bind.execute(
            sa.select(ROLE_PERMISSIONS.c.id, ROLE_PERMISSIONS.c.role_id, ROLE_PERMISSIONS.c.permission_id)
            .where(ROLE_PERMISSIONS.c.permission_id.in_(removed_ids)).order_by(ROLE_PERMISSIONS.c.id)
        ).all()
        for grant in grants:
            _record(REMOVED_GRANT, grant.id, {"id": grant.id, "role_id": grant.role_id, "permission_id": grant.permission_id})
        for row in removed:
            _record(REMOVED_PERMISSION, row.id, {"id": row.id, "name": row.name, "display_name": row.display_name,
                                                 "description": row.description})
        bind.execute(sa.delete(ROLE_PERMISSIONS).where(ROLE_PERMISSIONS.c.permission_id.in_(removed_ids)))
        bind.execute(sa.delete(PERMISSIONS).where(PERMISSIONS.c.id.in_(removed_ids)))
        _log(f"removed {', '.join(row.name for row in removed)} and {len(grants)} grant(s) of them")

    # In the transaction of the rows above: committed with them (on MySQL by the first RENAME at the latest), so a
    # rerun after a failed or interrupted rename never plans again from the converted data
    _record(CONVERTED, None, {"users": len(plan.users), "derived_roles": len(plan.derived_roles)})


# ---------------------------------------------------------------------------------------------------- downgrade
def downgrade() -> None:
    tables = _tables()
    if USERS not in tables:
        return
    _restore_legacy_tables(tables)
    # The assignments refer to the derived roles: dropped first (the table takes its indexes and foreign keys along)
    if ASSIGNMENTS in tables:
        _log(f"dropping {ASSIGNMENTS}")
        op.drop_table(ASSIGNMENTS)
    if LOG in tables:
        _replay_log()
        _log(f"dropping {LOG}")
        op.drop_table(LOG)
    if USER_TYPE in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(USERS)}:
        _log(f"dropping column {USERS}.{USER_TYPE}")
        with op.batch_alter_table(USERS) as batch:
            batch.drop_column(USER_TYPE)


def _restore_legacy_tables(tables: set) -> None:
    if USER_ROLES not in tables:
        if USER_ROLES_V1 in tables:
            _log(f"renaming {USER_ROLES_V1} -> {USER_ROLES}")
            op.rename_table(USER_ROLES_V1, USER_ROLES)
        else:
            # A fresh install never had it: the schema the models of 022 had, empty
            _log(f"creating {USER_ROLES}")
            op.create_table(
                USER_ROLES,
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
                sa.Column("assigned_at", sa.DateTime(), nullable=False),
                sa.UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),
            )
            op.create_index("ix_user_roles_user_id", USER_ROLES, ["user_id"])
            op.create_index("ix_user_roles_role_id", USER_ROLES, ["role_id"])
    if POST_ACCESS not in tables:
        if POST_ACCESS_V1 in tables:
            _log(f"renaming {POST_ACCESS_V1} -> {POST_ACCESS}")
            op.rename_table(POST_ACCESS_V1, POST_ACCESS)
        else:
            _log(f"creating {POST_ACCESS}")
            op.create_table(
                POST_ACCESS,
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
                sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
                sa.Column("can_write", sa.Boolean(), nullable=False),
                sa.Column("created_at", sa.DateTime(), nullable=False),
                sa.UniqueConstraint("user_id", "post_id", name="uq_user_post_access_user_post"),
            )
            op.create_index("ix_user_post_access_post_id", POST_ACCESS, ["post_id"])
            op.create_index("ix_user_post_access_user_id", POST_ACCESS, ["user_id"])


def _replay_log() -> None:
    """Undoes the conversion from the log: derived roles and inserted permissions out (with every grant of them),
    the removed permission rows and grants back with their ids (an id taken since gets a new one, grants follow it;
    a grant of a role deleted since is left out)"""
    bind = op.get_bind()
    entries: Dict[str, List] = {}
    for kind, ref_id, data in bind.execute(sa.select(LOG_ROWS.c.kind, LOG_ROWS.c.ref_id, LOG_ROWS.c.data)
                                           .order_by(LOG_ROWS.c.id)):
        entries.setdefault(kind, []).append((ref_id, data))

    derived = [ref_id for ref_id, _ in entries.get(DERIVED_ROLE, [])]
    if derived:
        _log(f"deleting {len(derived)} derived role(s)")
        bind.execute(sa.delete(ROLE_PERMISSIONS).where(ROLE_PERMISSIONS.c.role_id.in_(derived)))
        bind.execute(sa.delete(ROLES).where(ROLES.c.id.in_(derived)))
    added = [ref_id for ref_id, _ in entries.get(ADDED_PERMISSION, [])]
    if added:
        _log(f"deleting {len(added)} permission(s) the upgrade inserted")
        bind.execute(sa.delete(ROLE_PERMISSIONS).where(ROLE_PERMISSIONS.c.permission_id.in_(added)))
        bind.execute(sa.delete(PERMISSIONS).where(PERMISSIONS.c.id.in_(added)))

    new_ids: Dict[int, int] = {}
    for _, row in entries.get(REMOVED_PERMISSION, []):
        present = bind.execute(sa.select(PERMISSIONS.c.id).where(PERMISSIONS.c.name == row["name"])).first()
        if present is not None:
            new_ids[row["id"]] = present[0]
            continue
        values = {key: row[key] for key in ("name", "display_name", "description")}
        if bind.execute(sa.select(PERMISSIONS.c.id).where(PERMISSIONS.c.id == row["id"])).first() is None:
            bind.execute(PERMISSIONS.insert().values(id=row["id"], **values))
            new_ids[row["id"]] = row["id"]
        else:
            new_ids[row["id"]] = bind.execute(PERMISSIONS.insert().values(**values)).inserted_primary_key[0]
        _log(f"restored permission {row['name']}")
    roles = {role_id for (role_id,) in bind.execute(sa.select(ROLES.c.id))}
    restored = 0
    for _, row in entries.get(REMOVED_GRANT, []):
        if row["role_id"] not in roles or row["permission_id"] not in new_ids:
            continue
        values = {"role_id": row["role_id"], "permission_id": new_ids[row["permission_id"]]}
        if bind.execute(sa.select(ROLE_PERMISSIONS.c.id).where(
                ROLE_PERMISSIONS.c.role_id == values["role_id"],
                ROLE_PERMISSIONS.c.permission_id == values["permission_id"])).first() is not None:
            continue
        if bind.execute(sa.select(ROLE_PERMISSIONS.c.id).where(ROLE_PERMISSIONS.c.id == row["id"])).first() is None:
            values["id"] = row["id"]
        bind.execute(ROLE_PERMISSIONS.insert().values(**values))
        restored += 1
    if restored:
        _log(f"restored {restored} grant(s) of the removed permissions")
