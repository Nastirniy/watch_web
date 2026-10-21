"""No foreign keys from the "who acted" columns to users.

audit_logs.user_id, user_certificates.created_by and user_certificates.revoked_by name the user who acted. Their
foreign keys made every insert or update of such a row take a shared lock on the users row of the actor. A live race
run on MariaDB 11.8 (innodb_snapshot_isolation) showed what that costs: two admins acting on each other (ending each
other's sessions, issuing certificates for each other) deadlocked (1213) in every round, and export against revoke
or login against deactivation answered 500 (1020), because the other request held or had changed that users row.
Users are never hard-deleted (archive only), so these links protected nothing. The columns stay, with the same
values and the same meaning; only the constraints go. user_certificates.user_id (the owner) and every other foreign
key are unchanged (role_assignments.created_by has had none since 023).

Indexes: ix_audit_logs_user_id stays (the audit search filters by it). No code filters by created_by or
revoked_by, so no index is created for them; on MariaDB / MySQL the index the foreign key made automatically stays
where it is (dropping a foreign key never drops its index) and is harmless.

Guarded with inspector checks: a fresh install is built from the models by 002_sync_schema and has no such foreign
key; each one is found by constrained column and referred table (its name differs per database) and dropped only
where it exists. The downgrade creates the three foreign keys again (fk_<table>_<column>), only where missing.
Every actor id still names a users row (users are never deleted), so the downgrade finds no orphan to refuse.

Revision ID: 028_no_actor_foreign_keys
Revises: 027_scope_group_kinds
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa

revision = "028_no_actor_foreign_keys"
down_revision = "027_scope_group_kinds"
branch_labels = None
depends_on = None

USERS = "users"
ACTOR_COLUMNS = (
    ("audit_logs", "user_id"),
    ("user_certificates", "created_by"),
    ("user_certificates", "revoked_by"),
)
# sqlite reflects its foreign keys without a name; batch mode needs one to drop the constraint
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s"}


def _log(message: str) -> None:
    print(f"[028_no_actor_foreign_keys] {message}")


def _foreign_keys(table: str, column: str) -> list:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return []
    return [fk for fk in inspector.get_foreign_keys(table)
            if fk["referred_table"] == USERS and fk["constrained_columns"] == [column]]


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names() and column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for table, column in ACTOR_COLUMNS:
        for fk in _foreign_keys(table, column):
            name = fk["name"] or NAMING["fk"] % {"table_name": table, "column_0_name": column}
            _log(f"dropping foreign key {name} on {table}.{column}")
            with op.batch_alter_table(table, naming_convention=NAMING) as batch:
                batch.drop_constraint(name, type_="foreignkey")


def downgrade() -> None:
    for table, column in ACTOR_COLUMNS:
        if not _has_column(table, column) or _foreign_keys(table, column):
            continue
        name = NAMING["fk"] % {"table_name": table, "column_0_name": column}
        _log(f"creating foreign key {name} on {table}.{column}")
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.create_foreign_key(name, USERS, [column], ["id"])
