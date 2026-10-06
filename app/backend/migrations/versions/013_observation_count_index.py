"""Composite index for observation counters of a post.

Counters (GET /posts/{id}/observation-stats, GET /statistics)
filter observations by post_id and is_deleted and a created_at range. The
single-column indexes on post_id and created_at each serve only one of those
conditions; this index serves all three in one range scan.

Guarded with an inspector check: 002_sync_schema creates the index from the
model on fresh installs.

Revision ID: 013_observation_count_index
Revises: 012_observation_frequencies
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "013_observation_count_index"
down_revision = "012_observation_frequencies"
branch_labels = None
depends_on = None

TABLE = "observations"
INDEX = "ix_observations_post_deleted_created"
COLUMNS = ["post_id", "is_deleted", "created_at"]


def _log(message: str) -> None:
    print(f"[013_observation_count_index] {message}")


def _index_names(bind) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if INDEX in _index_names(bind):
        return
    _log(f"creating index {INDEX} on {TABLE}({', '.join(COLUMNS)})")
    op.create_index(INDEX, TABLE, COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in _index_names(bind):
        op.drop_index(INDEX, table_name=TABLE)
