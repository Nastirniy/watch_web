"""Built-in roles: roles.builtin_key (unique) marks the role that belongs to a user type.

Adds roles.builtin_key (max 50, unique index ix_roles_builtin_key). Seeding creates one built-in role per user type
(viewer, operator, senior_operator, analyst, post_admin, admin) and keeps its display texts and permissions equal to
the code at every start. Here:

- the role named "admin" becomes the built-in admin role (builtin_key = 'admin');
- a custom role that already uses another built-in key as its name is renamed to "<key>_custom" (a numeric suffix
  when that is taken too), so seeding can create the built-in role under the key. Nothing else about the role changes.

Guarded with inspector checks: fresh installs built by 002_sync_schema from the models already have the column
and the index. Running it again changes nothing.

Revision ID: 022_builtin_roles
Revises: 021_scope_groups
Create Date: 2026-10-12
"""
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


def _log(message: str) -> None:
    print(f"[022_builtin_roles] {message}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE)}:
        _log(f"adding column {TABLE}.{COLUMN}")
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=50), nullable=True))

    roles = sa.table(TABLE, sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column(COLUMN, sa.String))
    taken = {name for (name,) in bind.execute(sa.select(roles.c.name))}
    for key in OTHER_KEYS:
        rows = bind.execute(sa.select(roles.c.id).where(roles.c.name == key, roles.c[COLUMN].is_(None))).all()
        for (role_id,) in rows:
            new_name = f"{key}_custom"
            suffix = 2
            while new_name in taken:
                new_name = f"{key}_custom_{suffix}"
                suffix += 1
            _log(f"renaming custom role {key} (id {role_id}) -> {new_name}")
            bind.execute(sa.update(roles).where(roles.c.id == role_id).values(name=new_name))
            taken.add(new_name)

    admin_rows = bind.execute(
        sa.select(roles.c.id).where(roles.c.name == ADMIN_KEY, roles.c[COLUMN].is_(None))
    ).all()
    if admin_rows:
        already = bind.execute(sa.select(roles.c.id).where(roles.c[COLUMN] == ADMIN_KEY)).first()
        if already is None:
            _log("marking the admin role as built-in")
            bind.execute(sa.update(roles).where(roles.c.name == ADMIN_KEY).values(**{COLUMN: ADMIN_KEY}))

    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        _log(f"creating unique index {INDEX}")
        op.create_index(INDEX, TABLE, [COLUMN], unique=True)


def downgrade() -> None:
    """Drops the unique index and the column only. Roles renamed to <key>_custom keep their new names and the
    built-in roles seeding created stay as ordinary roles (seeding creates them again on the next start of the
    newer code)."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if INDEX in {i["name"] for i in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
