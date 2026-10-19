"""user_sessions.refresh_jti: a refresh token works once.

The session stores the jti of the one refresh token that may refresh it; every refresh replaces it
(auth/sessions.py, claim_refresh), so a copy of an older token refreshes nothing. Before, any refresh token of a
live session worked until its own expiry.

Sessions opened before the upgrade keep an empty refresh_jti: their first refresh, with any of their tokens, sets
it, so nobody is logged out. The column comes from the models on a fresh install (002_sync_schema), so it is added
only where it is missing. The downgrade drops it; older code ignores it anyway.

Revision ID: 025_session_refresh_jti
Revises: 024_user_certificates
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "025_session_refresh_jti"
down_revision = "024_user_certificates"
branch_labels = None
depends_on = None

TABLE = "user_sessions"
COLUMN = "refresh_jti"


def _columns() -> set:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN not in _columns():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    if COLUMN in _columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
