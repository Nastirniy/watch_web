"""The schema contract of a migration (RELEASE.md, "Release rules for developers").

Every revision after the baseline (bootstrap/schema_range.BASELINE) declares at module level
`CONTRACT = False` (expand: adds only, the previous release's code keeps running on the new schema) or
`CONTRACT = True` (contract: removes or narrows something). A contract revision calls, in upgrade(),

    from app.backend.migrations.support.compat import set_min_code_head
    set_min_code_head(op, "<revision>")

with the oldest code head that still runs on the schema it leaves: the previous release's head when it removes only
what that release no longer reads (the rollback by tag keeps working), its own revision when older code would break
(a rollback then needs the database restore). Its downgrade() puts back the value before it, or None when there was
none. tests/migrations/test_migration_policy.py enforces the marker.

schema_compat has no model and no migration of its own: one row (id 1), created here when missing. An absent table
means no contract since the baseline. It is in UNMODELLED_TABLES (support/autogenerate.py).
"""
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

TABLE_NAME = "schema_compat"
_metadata = sa.MetaData()
schema_compat = sa.Table(
    TABLE_NAME, _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("min_code_head", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
)


def read_min_code_head(conn: sa.Connection) -> Optional[str]:
    """The stored min_code_head; None without the table or its row"""
    if not sa.inspect(conn).has_table(TABLE_NAME):
        return None
    return conn.execute(sa.select(schema_compat.c.min_code_head).where(schema_compat.c.id == 1)).scalar()


def write_min_code_head(conn: sa.Connection, revision: Optional[str]) -> None:
    """Creates the table when missing and stores revision (None removes the row: no contract)"""
    if not sa.inspect(conn).has_table(TABLE_NAME):
        if revision is None:
            return
        schema_compat.create(conn)
    conn.execute(sa.delete(schema_compat).where(schema_compat.c.id == 1))
    if revision is not None:
        conn.execute(sa.insert(schema_compat).values(
            id=1, min_code_head=revision, updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))


def set_min_code_head(op, revision: Optional[str]) -> None:
    """In a migration: the oldest code head that runs on the schema this revision leaves (None: no contract)"""
    write_min_code_head(op.get_bind(), revision)
