"""users.pki_certificate_cn_hash_scheme: which rule produced pki_certificate_cn_hash.

Scheme 1 is the old rule (hash_data: colons and dashes removed, lower case) that was also applied to the certificate
name, so two names differing only by case or dashes collided on the unique hash. Scheme 2 (hash_cn) keeps case,
dashes and colons (Unicode NFC, surrounding whitespace stripped). The name itself is not stored, so an old hash
cannot be recomputed: existing rows stay scheme 1 (the server default) and keep logging in; a row moves to scheme 2
only when its certificate is replaced. Every certificate stored from now on is scheme 2.

Guarded with an inspector check: fresh installs built by 002_sync_schema from the models already have the column.

Revision ID: 019_certificate_cn_hash_scheme
Revises: 018_media_file_size_bigint
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "019_certificate_cn_hash_scheme"
down_revision = "018_media_file_size_bigint"
branch_labels = None
depends_on = None

TABLE = "users"
COLUMN = "pki_certificate_cn_hash_scheme"


def _log(message: str) -> None:
    print(f"[019_certificate_cn_hash_scheme] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        return
    _log(f"adding column {TABLE}.{COLUMN}")
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.SmallInteger(), nullable=False, server_default="1"))


def downgrade() -> None:
    """Drops the column. Rows of scheme 2 keep their hash, but the older code compares the old rule, so those
    users cannot log in until their certificate is replaced."""
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names() and COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
