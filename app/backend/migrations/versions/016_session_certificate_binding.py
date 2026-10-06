"""Bind sessions to the client certificate they were opened with: user_sessions.certificate_fingerprint_hash.

A production login (through nginx with ssl_verify_client) stores the SHA-256 of the certificate fingerprint;
every later request and refresh of that session must present the same certificate (RFC 8705 style), so a token
is useless without the certificate's private key. Existing sessions stay unbound (empty) and keep working until
they end; development logins stay unbound.

Guarded with an inspector check: fresh installs built by 002_sync_schema from the models already have the column.

Revision ID: 016_session_certificate_binding
Revises: 015_session_end_state
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "016_session_certificate_binding"
down_revision = "015_session_end_state"
branch_labels = None
depends_on = None

TABLE = "user_sessions"
COLUMN = "certificate_fingerprint_hash"


def _log(message: str) -> None:
    print(f"[016_session_certificate_binding] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        return
    _log(f"adding column {TABLE}.{COLUMN}")
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
