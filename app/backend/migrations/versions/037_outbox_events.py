"""Real-time events: the table outbox_events (API.md "Real-time events").

outbox_events: id BIGINT UNSIGNED auto-increment (2^31 ids are 250 days at 100 a second), type VARCHAR(48) (EventType),
v SMALLINT (payload version), scope_key VARCHAR(24) null (root, g:<id>, p:<id>; null: personal only), permission
VARCHAR(48) null, user_id INT null (also this user), conflate_key VARCHAR(48) null, payload VARCHAR(512) (JSON of the
catalog's typed params, enums/events.py keeps every event below it), occurred_at DATETIME(6) default the database
clock in UTC, UTC_TIMESTAMP(6) whatever the session time zone, like every time the application stores (lag and
retention are computed against the same clock).

A row is written in the transaction of the change (services/events/publish.py), read by every api worker and
deleted after events.retention_seconds by the media worker: delivery data, never a record.

No foreign key: a key would lock the parent row on every insert (migration 028). No index but the primary key: every
insert pays for each index, and every read is by id (the reader) or walks the oldest ids (the cleaner).

The table is listed in TABLES_OWNED_BY_LATER_REVISIONS of 002_sync_schema and the create is guarded with an inspector
check: where it exists already (a database built with create_all, a second run) it is left as it is.

Downgrade drops the table: its rows are delivery data, nothing is lost that the other tables do not hold.

Revision ID: 037_outbox_events
Revises: 036_webm_media_types
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "037_outbox_events"
down_revision = "036_webm_media_types"
branch_labels = None
depends_on = None

TABLE = "outbox_events"


def _log(message: str) -> None:
    print(f"[037_outbox_events] {message}")


def upgrade() -> None:
    if TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        _log(f"{TABLE} exists already, left as it is")
        return
    _log(f"creating {TABLE}")
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite").with_variant(
            mysql.BIGINT(unsigned=True), "mysql").with_variant(mysql.BIGINT(unsigned=True), "mariadb"),
            primary_key=True, autoincrement=True),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("v", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("scope_key", sa.String(length=24), nullable=True),
        sa.Column("permission", sa.String(length=48), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("conflate_key", sa.String(length=48), nullable=True),
        sa.Column("payload", sa.String(length=512), nullable=False),
        sa.Column("occurred_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql").with_variant(
            mysql.DATETIME(fsp=6), "mariadb"), nullable=False,
            server_default=sa.text("(UTC_TIMESTAMP(6))") if op.get_bind().dialect.name in ("mysql", "mariadb")
            else sa.text("CURRENT_TIMESTAMP")),
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    if TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    count = op.get_bind().execute(sa.text(f"select count(*) from {TABLE}")).scalar()
    _log(f"dropping {TABLE} ({count} rows of delivery data: events are hints, nothing else is lost)")
    op.drop_table(TABLE)
