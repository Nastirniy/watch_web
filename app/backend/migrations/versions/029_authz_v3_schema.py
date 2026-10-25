"""Authorization v3, schema only: permission levels, role kinds, system role assignments, the conversion log.

permissions.level and roles.kind: VARCHAR(10) NOT NULL, server default 'tree' (030_authorization_v3 writes the real
values from the frozen catalog). system_role_assignments: user + system role, no node, unique per user and role.
authz_v3_migration_log (kind, ref_id, data): what the downgrade of 030 needs. Nothing is converted here.

Guarded with inspector checks: fresh installs built by 002_sync_schema may already have the columns (002 creates
permissions and roles from the models) and list the two tables as owned by this revision.

Downgrade drops the tables and the columns.

Revision ID: 029_authz_v3_schema
Revises: 028_no_actor_foreign_keys
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = "029_authz_v3_schema"
down_revision = "028_no_actor_foreign_keys"
branch_labels = None
depends_on = None

PERMISSIONS, LEVEL = "permissions", "level"
ROLES, KIND = "roles", "kind"
SYSTEM_ASSIGNMENTS = "system_role_assignments"
UNIQUE = "uq_system_role_assignments_user_role"
INDEXED = ("user_id", "role_id")
LOG = "authz_v3_migration_log"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set:
    return {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    tables = set(_inspector().get_table_names())
    if PERMISSIONS in tables and LEVEL not in _columns(PERMISSIONS):
        op.add_column(PERMISSIONS, sa.Column(LEVEL, sa.String(length=10), nullable=False, server_default="tree"))
    if ROLES in tables and KIND not in _columns(ROLES):
        op.add_column(ROLES, sa.Column(KIND, sa.String(length=10), nullable=False, server_default="tree"))
    if SYSTEM_ASSIGNMENTS not in tables:
        op.create_table(
            SYSTEM_ASSIGNMENTS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.UniqueConstraint("user_id", "role_id", name=UNIQUE),
        )
    indexes = {i["name"] for i in _inspector().get_indexes(SYSTEM_ASSIGNMENTS)}
    for column in INDEXED:
        name = f"ix_{SYSTEM_ASSIGNMENTS}_{column}"
        if name not in indexes:
            op.create_index(name, SYSTEM_ASSIGNMENTS, [column])
    if LOG not in tables:
        op.create_table(
            LOG,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    if LOG in tables:
        op.drop_table(LOG)
    if SYSTEM_ASSIGNMENTS in tables:
        op.drop_table(SYSTEM_ASSIGNMENTS)
    if ROLES in tables and KIND in _columns(ROLES):
        with op.batch_alter_table(ROLES) as batch:
            batch.drop_column(KIND)
    if PERMISSIONS in tables and LEVEL in _columns(PERMISSIONS):
        with op.batch_alter_table(PERMISSIONS) as batch:
            batch.drop_column(LEVEL)
