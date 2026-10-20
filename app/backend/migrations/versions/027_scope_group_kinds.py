"""Scope groups get a kind (plain group, corps, brigade); observation_units records the units of an observation.

scope_groups.kind: VARCHAR(20) NOT NULL, server default 'group', indexed; every existing group is a plain group.
observation_units: the units above the post of an observation at its create (observation_id, group_id, created_at of
the observation), primary key (group_id, created_at, observation_id), index (observation_id, group_id), no foreign
keys. No backfill: before this revision no corps or brigade exists, so no observation has units.

Guarded with inspector checks: fresh installs built by 002_sync_schema may already have the column (002 creates
scope_groups from the models when it creates posts) and list observation_units as owned by this revision.

Revision ID: 027_scope_group_kinds
Revises: 026_users_no_hot_columns
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = "027_scope_group_kinds"
down_revision = "026_users_no_hot_columns"
branch_labels = None
depends_on = None

GROUPS = "scope_groups"
KIND = "kind"
KIND_INDEX = "ix_scope_groups_kind"
UNITS = "observation_units"
UNITS_INDEX = "ix_observation_units_observation"


def _log(message: str) -> None:
    print(f"[027_scope_group_kinds] {message}")


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    inspector = _inspector()
    if GROUPS in inspector.get_table_names():
        if KIND not in {c["name"] for c in inspector.get_columns(GROUPS)}:
            _log(f"adding column {GROUPS}.{KIND}")
            with op.batch_alter_table(GROUPS) as batch:
                batch.add_column(sa.Column(KIND, sa.String(length=20), nullable=False, server_default="group"))
        if KIND_INDEX not in {i["name"] for i in _inspector().get_indexes(GROUPS)}:
            op.create_index(KIND_INDEX, GROUPS, [KIND])

    if UNITS not in _inspector().get_table_names():
        _log(f"creating {UNITS}")
        op.create_table(
            UNITS,
            sa.Column("group_id", sa.Integer(), nullable=False, autoincrement=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("observation_id", sa.Integer(), nullable=False, autoincrement=False),
            sa.PrimaryKeyConstraint("group_id", "created_at", "observation_id"),
        )
    if UNITS_INDEX not in {i["name"] for i in _inspector().get_indexes(UNITS)}:
        op.create_index(UNITS_INDEX, UNITS, ["observation_id", "group_id"])


def downgrade() -> None:
    inspector = _inspector()
    if UNITS in inspector.get_table_names():
        op.drop_table(UNITS)
    if GROUPS in inspector.get_table_names() and KIND in {c["name"] for c in inspector.get_columns(GROUPS)}:
        if KIND_INDEX in {i["name"] for i in inspector.get_indexes(GROUPS)}:
            op.drop_index(KIND_INDEX, table_name=GROUPS)
        with op.batch_alter_table(GROUPS) as batch:
            batch.drop_column(KIND)
