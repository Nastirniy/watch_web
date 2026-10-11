"""media_files.file_size becomes BIGINT.

Uploads allow files of 10 GB, a MySQL INT stops at 2147483647 bytes (2 GiB): storing a bigger file answered 500.
Only MySQL needs the alter (sqlite INTEGER is 64-bit). The alter is skipped when the column is already BIGINT
(fresh installs: 002_sync_schema builds it from the model) and keeps every row.

Downgrade turns the column back into INT. It first reads MAX(file_size) and refuses (RuntimeError, nothing changed)
while a stored size does not fit an INT: a strict sql_mode would fail the ALTER, a non-strict one would clamp the
sizes to 2147483647 with only a warning.

Revision ID: 018_media_file_size_bigint
Revises: 017_video_storyboards
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "018_media_file_size_bigint"
down_revision = "017_video_storyboards"
branch_labels = None
depends_on = None

TABLE = "media_files"
COLUMN = "file_size"
INT_MAX = 2147483647


def _log(message: str) -> None:
    print(f"[018_media_file_size_bigint] {message}")


def needs_bigint(column_type, dialect_name: str) -> bool:
    """MySQL column that is a plain integer, not yet a BIGINT"""
    return dialect_name != "sqlite" and not isinstance(column_type, sa.BigInteger) and isinstance(column_type, sa.Integer)


def needs_integer(column_type, dialect_name: str) -> bool:
    return dialect_name != "sqlite" and isinstance(column_type, sa.BigInteger)


def _column_type(bind):
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return None
    for column in inspector.get_columns(TABLE):
        if column["name"] == COLUMN:
            return column["type"]
    return None


def upgrade() -> None:
    bind = op.get_bind()
    column_type = _column_type(bind)
    if column_type is not None and needs_bigint(column_type, bind.dialect.name):
        _log(f"altering {TABLE}.{COLUMN} to BIGINT")
        op.alter_column(TABLE, COLUMN, type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=False)


def check_sizes_fit_integer(bind) -> None:
    """Refuse the downgrade while a stored size is above INT_MAX (MySQL in a non-strict sql_mode would clamp it)"""
    largest = bind.execute(sa.text(f"SELECT MAX({COLUMN}) FROM {TABLE}")).scalar()
    if largest is not None and largest > INT_MAX:
        raise RuntimeError(
            f"018_media_file_size_bigint downgrade refused: {TABLE}.{COLUMN} holds {largest} bytes, an INT holds at"
            f" most {INT_MAX}. Keep this revision, or remove the media rows larger than 2 GiB first."
        )


def downgrade() -> None:
    bind = op.get_bind()
    column_type = _column_type(bind)
    if column_type is not None and needs_integer(column_type, bind.dialect.name):
        check_sizes_fit_integer(bind)
        _log(f"altering {TABLE}.{COLUMN} back to INTEGER")
        op.alter_column(TABLE, COLUMN, type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=False)
