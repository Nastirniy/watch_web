"""Queued moves: the tables pending_moves and move_locks, upload_sessions.last_chunk_at.

pending_moves: a move of a post or a group that waits for the uploads below it. node_kind VARCHAR(8) (post, group),
node_id INT, target_group_id INT NULL (null: directly under the root), requested_by INT, requested_at DATETIME,
status VARCHAR(12) (waiting, done, cancelled, failed), force BOOL, finished_at DATETIME NULL, error VARCHAR(64) NULL
(the problem code of a failed move). Index ix_pending_moves_waiting (status, node_kind, node_id): every new upload
reads the waiting moves. No foreign keys: node_id names a post or a group, requested_by is an actor column (migration
028) and a move outlives what it names. No CHECK constraints.

move_locks: one row, id 1. It is locked first by every write that moves a node, so moves run one after another
whatever nodes they touch (services/move_queue.py lock_moves). No other row is ever written.

upload_sessions.last_chunk_at DATETIME NULL: the last time bytes of a chunk arrived; last_activity_at does not say
that (every claim moves it, with or without bytes). Rows of sessions that have received bytes start from
last_activity_at, which is the nearest thing they have.

Both tables are listed in TABLES_OWNED_BY_LATER_REVISIONS of 002_sync_schema, and every create here is guarded with
an inspector check. The column comes from the model on a fresh install (002_sync_schema), so it is added only where it
is missing.

Downgrade drops both tables and the column. A waiting move is lost with its table, and the uploads below it were never
held by anything but that row. The output counts the rows.

Revision ID: 035_queued_moves
Revises: 034_session_refresh_reuse
Create Date: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa

revision = "035_queued_moves"
down_revision = "034_session_refresh_reuse"
branch_labels = None
depends_on = None

MOVES = "pending_moves"
LOCKS = "move_locks"
UPLOADS = "upload_sessions"
COLUMN = "last_chunk_at"
INDEX = "ix_pending_moves_waiting"


def _log(message: str) -> None:
    print(f"[035_queued_moves] {message}")


def _inspector():
    return sa.inspect(op.get_bind())


def _create_moves() -> None:
    op.create_table(
        MOVES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("node_kind", sa.String(length=8), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.Column("target_group_id", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(length=64), nullable=True),
    )


def upgrade() -> None:
    tables = set(_inspector().get_table_names())
    if MOVES in tables:
        _log(f"{MOVES} exists already, left as it is")
    else:
        _log(f"creating {MOVES}")
        _create_moves()
    if INDEX not in {index["name"] for index in _inspector().get_indexes(MOVES)}:
        op.create_index(INDEX, MOVES, ["status", "node_kind", "node_id"])
    if LOCKS not in tables:
        _log(f"creating {LOCKS}")
        op.create_table(LOCKS, sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False))
    bind = op.get_bind()
    if bind.execute(sa.text(f"select count(*) from {LOCKS}")).scalar() == 0:
        bind.execute(sa.text(f"insert into {LOCKS} (id) values (1)"))

    if COLUMN not in {column["name"] for column in _inspector().get_columns(UPLOADS)}:
        op.add_column(UPLOADS, sa.Column(COLUMN, sa.DateTime(), nullable=True))
        count = bind.execute(sa.text(f"update {UPLOADS} set {COLUMN} = last_activity_at where received_bytes > 0")).rowcount
        _log(f"{UPLOADS}.{COLUMN} added, {count} rows start from last_activity_at")


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    bind = op.get_bind()
    for table in (LOCKS, MOVES):
        if table in tables:
            count = bind.execute(sa.text(f"select count(*) from {table}")).scalar()
            _log(f"dropping {table} ({count} rows)")
            op.drop_table(table)
    if COLUMN in {column["name"] for column in _inspector().get_columns(UPLOADS)}:
        with op.batch_alter_table(UPLOADS) as batch:
            batch.drop_column(COLUMN)
