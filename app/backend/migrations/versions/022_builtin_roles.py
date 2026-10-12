"""Built-in roles: roles.builtin_key (unique) marks the role that belongs to a user type.

Adds roles.builtin_key (max 50, unique index ix_roles_builtin_key). Seeding creates one built-in role per user type
(viewer, operator, senior_operator, analyst, post_admin, admin) and keeps its display texts and permissions equal to
the code at every start. Here:

- the role named "admin" becomes the built-in admin role (builtin_key = 'admin');
- a custom role that already uses another built-in key as its name is renamed to "<key>_custom" (a numeric suffix
  when that is taken too), so seeding can create the built-in role under the key. Nothing else about the role changes.
  Names compare without case, as roles.name does on MySQL and MariaDB (utf8mb4_unicode_ci, the unique index too): a
  legacy "Viewer" blocks the built-in "viewer" there and is renamed as well, and "Operator_custom" takes the suffix
  "operator_custom". The database's own comparison counts too, whatever its collation.

What the downgrade needs is recorded in builtin_roles_migration_log (kind, ref_id, data), in the transaction of the
change: every rename (the name as stored, its case included), the admin role it marks (its texts and archive flag, which seeding resets) and
the display texts of every permission present now (seeding refreshes them from the code).

Downgrade restores the roles and permissions exactly (spec section 8, controller ruling R31), also after the
application started and seeded: the built-in roles seeding created (builtin_key set, not admin) are deleted with
their grants, unless a legacy user_roles row refers to one (it is kept and a line says so); every recorded role gets
its old name back (unless another role holds that name now: a line says so); the admin role gets its recorded texts
back and keeps existing (on an upgraded database it was there before, this revision only marked it); the
permission texts come back;
media.monitor, which the code of this revision introduced, is deleted with its grants (023's log replay already
removes it when 023 inserted it). Then roles.builtin_key, its index and the log are dropped.

Guarded with inspector checks: fresh installs built by 002_sync_schema from the models already have the column
and the index. Running it again renames nothing and records only permissions that have no record yet.

Revision ID: 022_builtin_roles
Revises: 021_scope_groups
Create Date: 2026-10-12
"""
from typing import Dict, List, Optional, Tuple

from alembic import op
import sqlalchemy as sa

revision = "022_builtin_roles"
down_revision = "021_scope_groups"
branch_labels = None
depends_on = None

TABLE = "roles"
COLUMN = "builtin_key"
INDEX = "ix_roles_builtin_key"
ADMIN_KEY = "admin"
OTHER_KEYS = ("viewer", "operator", "senior_operator", "analyst", "post_admin")
LOG = "builtin_roles_migration_log"
# Permissions the code of this revision added to the catalog (021 owns scope_group.*)
ADDED_PERMISSIONS = ("media.monitor",)

# What the log records (kind: ref_id, data)
RENAMED_ROLE = "renamed_role"            # roles.id: name (before), renamed_to
MARKED_ADMIN = "marked_admin"            # roles.id: name, display_name, description, is_deleted (before)
PERMISSION_TEXTS = "permission_texts"    # permissions.id: name, display_name, description (before seeding)

ROLES = sa.table(
    TABLE, sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("display_name", sa.String),
    sa.column("description", sa.String), sa.column("is_deleted", sa.Boolean), sa.column(COLUMN, sa.String),
)
PERMISSIONS = sa.table(
    "permissions", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("display_name", sa.String),
    sa.column("description", sa.String),
)
ROLE_PERMISSIONS = sa.table("role_permissions", sa.column("role_id", sa.Integer), sa.column("permission_id", sa.Integer))
LOG_ROWS = sa.table(LOG, sa.column("id", sa.Integer), sa.column("kind", sa.String), sa.column("ref_id", sa.Integer),
                    sa.column("data", sa.JSON))


def _log(message: str) -> None:
    print(f"[022_builtin_roles] {message}")


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _record(kind: str, ref_id: int, data: dict) -> None:
    op.get_bind().execute(LOG_ROWS.insert().values(kind=kind, ref_id=ref_id, data=data))


def _name_in_use(name: str, other_than: Optional[int] = None) -> bool:
    """A role (other than the given one) holds the name, as the database compares names (its collation)"""
    query = sa.select(ROLES.c.id).where(ROLES.c.name == name)
    if other_than is not None:
        query = query.where(ROLES.c.id != other_than)
    return op.get_bind().execute(query.limit(1)).first() is not None


# ---------------------------------------------------------------------------------------------------- upgrade
def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if TABLE not in tables:
        return
    if LOG not in tables:
        _log(f"creating {LOG}")
        op.create_table(
            LOG,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
        )
    if COLUMN not in {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}:
        _log(f"adding column {TABLE}.{COLUMN}")
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=50), nullable=True))

    customs = bind.execute(sa.select(ROLES.c.id, ROLES.c.name).where(ROLES.c[COLUMN].is_(None))
                           .order_by(ROLES.c.id)).all()
    taken = {name.casefold() for (name,) in bind.execute(sa.select(ROLES.c.name))}
    for key in OTHER_KEYS:
        # what the database calls equal to the key (its collation) and what equals it without case (sqlite compares
        # with case: the same roles are renamed on every database)
        equal = {role_id for (role_id,) in bind.execute(
            sa.select(ROLES.c.id).where(ROLES.c.name == key, ROLES.c[COLUMN].is_(None)))}
        for role_id, name in [(role_id, name) for role_id, name in customs if role_id in equal or name.casefold() == key]:
            new_name = f"{key}_custom"
            suffix = 2
            while new_name.casefold() in taken or _name_in_use(new_name):
                new_name = f"{key}_custom_{suffix}"
                suffix += 1
            _log(f"renaming custom role {name} (id {role_id}) -> {new_name}")
            bind.execute(sa.update(ROLES).where(ROLES.c.id == role_id).values(name=new_name))
            _record(RENAMED_ROLE, role_id, {"name": name, "renamed_to": new_name})
            taken.add(new_name.casefold())

    admin_rows = bind.execute(
        sa.select(ROLES.c.id, ROLES.c.name, ROLES.c.display_name, ROLES.c.description, ROLES.c.is_deleted)
        .where(ROLES.c.name == ADMIN_KEY, ROLES.c[COLUMN].is_(None))
    ).all()
    if admin_rows:
        already = bind.execute(sa.select(ROLES.c.id).where(ROLES.c[COLUMN] == ADMIN_KEY)).first()
        if already is None:
            _log("marking the admin role as built-in")
            for role_id, name, display_name, description, is_deleted in admin_rows:
                _record(MARKED_ADMIN, role_id, {"name": name, "display_name": display_name,
                                                "description": description, "is_deleted": bool(is_deleted)})
            bind.execute(sa.update(ROLES).where(ROLES.c.name == ADMIN_KEY).values(**{COLUMN: ADMIN_KEY}))

    if "permissions" in tables:
        recorded = {ref_id for (ref_id,) in bind.execute(
            sa.select(LOG_ROWS.c.ref_id).where(LOG_ROWS.c.kind == PERMISSION_TEXTS))}
        texts = [row for row in bind.execute(sa.select(
            PERMISSIONS.c.id, PERMISSIONS.c.name, PERMISSIONS.c.display_name, PERMISSIONS.c.description
        ).order_by(PERMISSIONS.c.id)) if row[0] not in recorded]
        for permission_id, name, display_name, description in texts:
            _record(PERMISSION_TEXTS, permission_id,
                    {"name": name, "display_name": display_name, "description": description})
        if texts:
            _log(f"recorded the display texts of {len(texts)} permission(s)")

    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        _log(f"creating unique index {INDEX}")
        op.create_index(INDEX, TABLE, [COLUMN], unique=True)


# -------------------------------------------------------------------------------------------------- downgrade
def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if TABLE not in tables:
        return
    has_column = COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if has_column:
        _delete_seeded_roles(tables)
    entries = _entries() if LOG in tables else {}
    _restore_roles(entries)
    if "permissions" in tables:
        _restore_permission_texts(entries)
        _delete_added_permissions()

    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if has_column:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
    if LOG in tables:
        _log(f"dropping {LOG}")
        op.drop_table(LOG)


def _entries() -> Dict[str, List[Tuple[int, dict]]]:
    entries: Dict[str, List[Tuple[int, dict]]] = {}
    for kind, ref_id, data in op.get_bind().execute(
            sa.select(LOG_ROWS.c.kind, LOG_ROWS.c.ref_id, LOG_ROWS.c.data).order_by(LOG_ROWS.c.id)):
        entries.setdefault(kind, []).append((ref_id, data))
    return entries


def _referrers(tables: set, role_id: int) -> List[str]:
    """Tables whose rows still refer to the role (a legacy holder, or an assignment table that is still there)"""
    bind = op.get_bind()
    found = []
    for table in ("user_roles", "role_assignments"):
        if table in tables:
            rows = sa.table(table, sa.column("role_id", sa.Integer))
            if bind.execute(sa.select(rows.c.role_id).where(rows.c.role_id == role_id).limit(1)).first():
                found.append(table)
    return found


def _delete_seeded_roles(tables: set) -> None:
    """The built-in roles seeding created (every one but admin, which existed before on an upgraded database):
    deleted with their grants"""
    bind = op.get_bind()
    seeded = bind.execute(sa.select(ROLES.c.id, ROLES.c.name, ROLES.c[COLUMN]).where(
        ROLES.c[COLUMN].isnot(None), ROLES.c[COLUMN] != ADMIN_KEY).order_by(ROLES.c.id)).all()
    for role_id, name, key in seeded:
        referrers = _referrers(tables, role_id)
        if referrers:
            _log(f"keeping built-in role {name} (id {role_id}): rows of {', '.join(referrers)} refer to it")
            continue
        _log(f"deleting built-in role {name} (id {role_id}) created by seeding")
        bind.execute(sa.delete(ROLE_PERMISSIONS).where(ROLE_PERMISSIONS.c.role_id == role_id))
        bind.execute(sa.delete(ROLES).where(ROLES.c.id == role_id))


def _restore_roles(entries: Dict[str, List[Tuple[int, dict]]]) -> None:
    bind = op.get_bind()
    for role_id, data in entries.get(MARKED_ADMIN, []):
        if bind.execute(sa.select(ROLES.c.id).where(ROLES.c.id == role_id)).first() is None:
            continue
        _log(f"restoring the texts of the admin role (id {role_id})")
        bind.execute(sa.update(ROLES).where(ROLES.c.id == role_id).values(
            name=data["name"], display_name=data["display_name"], description=data["description"],
            is_deleted=bool(data["is_deleted"])))
    for role_id, data in entries.get(RENAMED_ROLE, []):
        current = bind.execute(sa.select(ROLES.c.name).where(ROLES.c.id == role_id)).first()
        if current is None:
            continue
        folded = data["name"].casefold()
        holder = [other_id for other_id, other in bind.execute(sa.select(ROLES.c.id, ROLES.c.name))
                  if other_id != role_id and other.casefold() == folded]
        if holder or _name_in_use(data["name"], other_than=role_id):
            _log(f"keeping the name {current[0]} of role {role_id}: another role is named {data['name']}")
            continue
        _log(f"renaming role {current[0]} (id {role_id}) back to {data['name']}")
        bind.execute(sa.update(ROLES).where(ROLES.c.id == role_id).values(name=data["name"]))


def _restore_permission_texts(entries: Dict[str, List[Tuple[int, dict]]]) -> None:
    bind = op.get_bind()
    restored = 0
    for permission_id, data in entries.get(PERMISSION_TEXTS, []):
        result = bind.execute(sa.update(PERMISSIONS).where(
            PERMISSIONS.c.id == permission_id, PERMISSIONS.c.name == data["name"]
        ).values(display_name=data["display_name"], description=data["description"]))
        restored += result.rowcount or 0
    if restored:
        _log(f"restored the display texts of {restored} permission(s)")


def _delete_added_permissions() -> None:
    bind = op.get_bind()
    for name in ADDED_PERMISSIONS:
        row = bind.execute(sa.select(PERMISSIONS.c.id).where(PERMISSIONS.c.name == name)).first()
        if row is None:
            continue
        _log(f"deleting permission {name} (id {row[0]}) and its grants")
        bind.execute(sa.delete(ROLE_PERMISSIONS).where(ROLE_PERMISSIONS.c.permission_id == row[0]))
        bind.execute(sa.delete(PERMISSIONS).where(PERMISSIONS.c.id == row[0]))
