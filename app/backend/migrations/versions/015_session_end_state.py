"""Session end state: user_sessions.end_reason and revoke_cause; user_sessions.is_active goes.

The state of a session (online / idle / ended) is computed when it is read, from ended_at, expires_at and
last_activity (services/sessions.py). is_active duplicated ended_at and read like a lock on the row rather than
a login state. Rows with is_active = false and no ended_at get ended_at = last_activity; their end_reason stays
empty (reasons were not recorded before this revision).

Guarded with inspector checks: fresh installs built by 002_sync_schema from the models already have the new
columns and never had is_active.

downgrade adds is_active back (true where ended_at is empty) and drops end_reason and revoke_cause.

Revision ID: 015_session_end_state
Revises: 014_drop_post_connection_columns
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "015_session_end_state"
down_revision = "014_drop_post_connection_columns"
branch_labels = None
depends_on = None

TABLE = "user_sessions"
ENDED_AT_INDEX = "ix_user_sessions_ended_at"
IS_ACTIVE_INDEX = "ix_user_sessions_is_active"


def _log(message: str) -> None:
    print(f"[015_session_end_state] {message}")


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind) -> dict:
    return {i["name"]: set(i.get("column_names") or []) for i in sa.inspect(bind).get_indexes(TABLE) if i.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return

    columns = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        if "end_reason" not in columns:
            _log(f"adding column {TABLE}.end_reason")
            batch.add_column(sa.Column("end_reason", sa.String(length=20), nullable=True))
        if "revoke_cause" not in columns:
            _log(f"adding column {TABLE}.revoke_cause")
            batch.add_column(sa.Column("revoke_cause", sa.String(length=32), nullable=True))

    if "is_active" in columns:
        ended = bind.execute(
            sa.text(f"UPDATE {TABLE} SET ended_at = last_activity WHERE is_active = :inactive AND ended_at IS NULL"),
            {"inactive": False},
        ).rowcount
        _log(f"ended_at set on {ended} inactive sessions")
        with op.batch_alter_table(TABLE) as batch:
            for name, index_columns in _indexes(bind).items():
                if "is_active" in index_columns:
                    _log(f"dropping index {name}")
                    batch.drop_index(name)
            _log(f"dropping column {TABLE}.is_active")
            batch.drop_column("is_active")

    if not any("ended_at" in index_columns for index_columns in _indexes(bind).values()):
        _log(f"creating index {ENDED_AT_INDEX}")
        op.create_index(ENDED_AT_INDEX, TABLE, ["ended_at"])


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    indexes = _indexes(bind)
    with op.batch_alter_table(TABLE) as batch:
        if "is_active" not in columns:
            batch.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")))
        if ENDED_AT_INDEX in indexes:
            batch.drop_index(ENDED_AT_INDEX)
        for name in ("end_reason", "revoke_cause"):
            if name in columns:
                batch.drop_column(name)
    bind.execute(sa.text(f"UPDATE {TABLE} SET is_active = :inactive WHERE ended_at IS NOT NULL"), {"inactive": False})
    if IS_ACTIVE_INDEX not in _indexes(bind):
        op.create_index(IS_ACTIVE_INDEX, TABLE, ["is_active"])
