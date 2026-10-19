"""users: no column a request writes on a hot path.

users.last_login_at was written by every login and users.last_record_created_at by every observation create. On
MariaDB 11.6+ with snapshot isolation a write to the users row collided with the same user's concurrent requests:
their foreign key checks and writes met a row changed after their snapshot (error 1020, answered 500), and two logins
of one user deadlocked. last_login_at is now derived from the login sessions (models/presence_properties.py: the
latest user_sessions.created_at), last_record_created_at was never read and is gone.

A fresh install has neither column (002_sync_schema builds users from the models), so each is dropped only where it
exists. The downgrade adds them back and fills last_login_at from the sessions; last_record_created_at stays empty.

Revision ID: 026_users_no_hot_columns
Revises: 025_session_refresh_jti
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "026_users_no_hot_columns"
down_revision = "025_session_refresh_jti"
branch_labels = None
depends_on = None

TABLE = "users"
LAST_LOGIN = "last_login_at"
LAST_RECORD = "last_record_created_at"
LAST_LOGIN_INDEX = "ix_users_last_login_at"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set:
    return {column["name"] for column in _inspector().get_columns(TABLE)}


def _indexes() -> set:
    return {index["name"] for index in _inspector().get_indexes(TABLE)}


def upgrade() -> None:
    present = _columns() & {LAST_LOGIN, LAST_RECORD}
    if not present:
        return
    if LAST_LOGIN_INDEX in _indexes():
        op.drop_index(LAST_LOGIN_INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        for column in sorted(present):
            batch.drop_column(column)


def downgrade() -> None:
    columns = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if LAST_LOGIN not in columns:
            batch.add_column(sa.Column(LAST_LOGIN, sa.DateTime(), nullable=True))
        if LAST_RECORD not in columns:
            batch.add_column(sa.Column(LAST_RECORD, sa.DateTime(), nullable=True))
    if LAST_LOGIN_INDEX not in _indexes():
        op.create_index(LAST_LOGIN_INDEX, TABLE, [LAST_LOGIN])
    op.execute(
        "UPDATE users SET last_login_at = "
        "(SELECT MAX(user_sessions.created_at) FROM user_sessions WHERE user_sessions.user_id = users.id)"
    )
