"""Role soft delete flag and unique role/permission assignments.

- roles.is_deleted (NOT NULL, default false) + index
- unique index on role_permissions(role_id, permission_id), duplicates removed first
- unique index on user_roles(user_id, role_id), duplicates removed first

Every step is guarded with an inspector check because 002_sync_schema already
creates the column and the unique constraints on fresh installs. A fresh install
has no user_roles table at all (023_authorization_v2 replaced it by role_assignments
and 002 builds the tables of the current models): that step is skipped.

Revision ID: 004_role_soft_delete
Revises: 003_add_user_post_access
Create Date: 2026-09-02
"""
from typing import List, Tuple

from alembic import op
import sqlalchemy as sa

revision = "004_role_soft_delete"
down_revision = "003_add_user_post_access"
branch_labels = None
depends_on = None

UNIQUE_ROLE_PERMISSIONS = "uq_role_permissions_role_permission"
UNIQUE_USER_ROLES = "uq_user_roles_user_role"


def _log(message: str) -> None:
    print(f"[004_role_soft_delete] {message}")


def _has_unique(inspector, table: str, columns: Tuple[str, ...]) -> bool:
    wanted = set(columns)
    for uc in inspector.get_unique_constraints(table):
        if set(uc.get("column_names", [])) == wanted:
            return True
    for idx in inspector.get_indexes(table):
        if idx.get("unique") and set(idx.get("column_names", [])) == wanted:
            return True
    return False


def _remove_duplicates(bind, table: str, columns: Tuple[str, ...]) -> int:
    """Keep the row with the smallest id per (columns) group, delete the rest. Portable, done in Python."""
    cols = ", ".join(columns)
    rows = bind.execute(sa.text(f"SELECT id, {cols} FROM {table} ORDER BY id")).fetchall()
    seen = set()
    to_delete: List[int] = []
    for row in rows:
        key = tuple(row[1:])
        if key in seen:
            to_delete.append(row[0])
        else:
            seen.add(key)
    for row_id in to_delete:
        bind.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
    return len(to_delete)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    role_columns = {c["name"] for c in inspector.get_columns("roles")}
    if "is_deleted" not in role_columns:
        _log("adding roles.is_deleted")
        op.add_column("roles", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    role_indexes = {i["name"] for i in inspector.get_indexes("roles")}
    if "ix_roles_is_deleted" not in role_indexes:
        op.create_index("ix_roles_is_deleted", "roles", ["is_deleted"])

    tables = set(inspector.get_table_names())
    for table, columns, name in (
        ("role_permissions", ("role_id", "permission_id"), UNIQUE_ROLE_PERMISSIONS),
        ("user_roles", ("user_id", "role_id"), UNIQUE_USER_ROLES),
    ):
        if table not in tables or _has_unique(inspector, table, columns):
            continue
        removed = _remove_duplicates(bind, table, columns)
        if removed:
            _log(f"removed {removed} duplicate rows from {table}")
        _log(f"creating unique index {name} on {table}({', '.join(columns)})")
        op.create_index(name, table, list(columns), unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, name in (("user_roles", UNIQUE_USER_ROLES), ("role_permissions", UNIQUE_ROLE_PERMISSIONS)):
        if table in tables and name in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
    if "ix_roles_is_deleted" in {i["name"] for i in inspector.get_indexes("roles")}:
        op.drop_index("ix_roles_is_deleted", table_name="roles")
    if "is_deleted" in {c["name"] for c in inspector.get_columns("roles")}:
        with op.batch_alter_table("roles") as batch:
            batch.drop_column("is_deleted")
