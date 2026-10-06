"""Decode HTML entities stored in user-entered text.

Until this revision the API ran html.escape on every incoming string, so the
tables hold values such as "O&#x27;Brien" (for "O'Brien") or "AT&amp;T". The API
now stores and returns plain text, and responses decode leftovers on the fly;
this data migration fixes the rows themselves so filters and LIKE searches
match the real characters again.

Only user-entered text columns are touched. A row is left alone (and logged)
when the decoded value would collide with another row on a unique column.

Revision ID: 007_unescape_html_entities
Revises: 006_drop_legacy_thumbnails
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

from app.backend.utils.text import unescape_html

revision = "007_unescape_html_entities"
down_revision = "006_drop_legacy_thumbnails"
branch_labels = None
depends_on = None

# table -> user-entered text columns
TEXT_COLUMNS = {
    "users": ("username_alias",),
    "posts": ("name", "code", "description", "region", "city"),
    "tags": ("name",),
    "categories": ("name", "alias"),
    "signal_types": ("code", "name", "description"),
    "observations": ("comment",),
    "roles": ("name", "display_name", "description"),
    "media_files": ("original_filename",),
}

UNIQUE_COLUMNS = {
    ("posts", "code"),
    ("tags", "name"),
    ("categories", "name"),
    ("signal_types", "code"),
    ("roles", "name"),
}


def _log(message: str) -> None:
    print(f"[007_unescape_html_entities] {message}")


def _repair_column(bind, table: str, column: str) -> int:
    rows = bind.execute(
        sa.text(f"SELECT id, {column} AS value FROM {table} WHERE {column} LIKE :pattern"),
        {"pattern": "%&%;%"},
    ).fetchall()
    fixed = 0
    for row in rows:
        old = row.value
        new = unescape_html(old)
        if new == old:
            continue
        if (table, column) in UNIQUE_COLUMNS:
            clash = bind.execute(
                sa.text(f"SELECT id FROM {table} WHERE {column} = :value AND id <> :id"),
                {"value": new, "id": row.id},
            ).fetchone()
            if clash is not None:
                _log(f"skipping {table}.{column} id={row.id}: decoded value already used by id={clash.id}")
                continue
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
            {"value": new, "id": row.id},
        )
        fixed += 1
    return fixed


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    total = 0
    for table, columns in TEXT_COLUMNS.items():
        if table not in existing_tables:
            continue
        existing_columns = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column not in existing_columns:
                continue
            fixed = _repair_column(bind, table, column)
            if fixed:
                _log(f"decoded {fixed} value(s) in {table}.{column}")
            total += fixed
    _log(f"done, {total} value(s) decoded")


def downgrade() -> None:
    # Data repair only: the decoded text is the correct content, nothing to undo.
    pass
