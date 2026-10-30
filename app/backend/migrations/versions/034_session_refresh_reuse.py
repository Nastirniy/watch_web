"""user_sessions.previous_refresh_jti and refresh_rotated_at: a used refresh token presented again ends its session.

Every refresh records the jti of the token it retired (previous_refresh_jti) and when (refresh_rotated_at), in the
same UPDATE that replaces refresh_jti (auth/sessions.py claim_refresh). A used token presented again ends the session
(RFC 9700 section 4.14, revoke_cause refresh_token_reused), except that previous token within
security.refresh_reuse_grace_seconds of the rotation (two tabs refreshing at once): refused, the session stays.

previous_refresh_jti VARCHAR(64) NULL, refresh_rotated_at DATETIME NULL, no index (read only with the session row by
its primary key). Sessions opened before the upgrade keep both empty until their next refresh: a used token presented
before that is answered as before (invalid_refresh_token, the session stays), so nobody is logged out by the upgrade.
The columns come from the models on a fresh install (002_sync_schema), so each is added only where it is missing.
The downgrade drops both; older code ignores them anyway.

Revision ID: 034_session_refresh_reuse
Revises: 033_user_guides
Create Date: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa

revision = "034_session_refresh_reuse"
down_revision = "033_user_guides"
branch_labels = None
depends_on = None

TABLE = "user_sessions"
COLUMNS = {
    "previous_refresh_jti": sa.String(length=64),
    "refresh_rotated_at": sa.DateTime(),
}


def _columns() -> set:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    present = _columns()
    for name, column_type in COLUMNS.items():
        if name not in present:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    present = _columns()
    gone = [name for name in COLUMNS if name in present]
    if gone:
        with op.batch_alter_table(TABLE) as batch:
            for name in gone:
                batch.drop_column(name)
