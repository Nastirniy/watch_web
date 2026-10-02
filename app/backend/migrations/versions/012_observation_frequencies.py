"""Frequency bands of an observation move to observation_frequencies.

observations had one fixed rc set (frequency_min/max/center, bandwidth) and one
video set (video_*). Each set becomes one row of observation_frequencies with
kind = rc / video, so an observation can hold any number of bands. When an
observation had both sets they described one emitter, so both rows get
link_group = 1. bandwidth is a generated column (frequency_max - frequency_min).

Before copying, rows that cannot satisfy the new constraints (a center or one
bound without the full min/max pair, min <= 0, max <= min) stop the migration
with their ids: fix or clear those values and run it again. Nothing is copied
or dropped in that case.

Guarded with inspector checks: 002_sync_schema builds fresh installs from the
models, where observations has no frequency columns and the new table is left
to this revision.

downgrade restores the columns from the first active band of each kind; any
further bands are lost.

Revision ID: 012_observation_frequencies
Revises: 011_upload_sessions
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "012_observation_frequencies"
down_revision = "011_upload_sessions"
branch_labels = None
depends_on = None

TABLE = "observation_frequencies"

# kind -> (min, max, center, bandwidth) columns on observations
LEGACY_SETS = {
    "rc": ("frequency_min", "frequency_max", "frequency_center", "bandwidth"),
    "video": ("video_frequency_min", "video_frequency_max", "video_frequency_center", "video_bandwidth"),
}
LEGACY_COLUMNS = [name for columns in LEGACY_SETS.values() for name in columns]
LEGACY_INDEXED = ("frequency_center", "video_frequency_center")


def _log(message: str) -> None:
    print(f"[012_observation_frequencies] {message}")


def _create_table(bind) -> None:
    if TABLE in sa.inspect(bind).get_table_names():
        return
    _log(f"creating {TABLE}")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("observations.id"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("link_group", sa.Integer(), nullable=True),
        sa.Column("frequency_min", sa.Numeric(12, 3), nullable=False),
        sa.Column("frequency_max", sa.Numeric(12, 3), nullable=False),
        sa.Column("frequency_center", sa.Numeric(12, 3), nullable=False),
        sa.Column("bandwidth", sa.Numeric(12, 3), sa.Computed("frequency_max - frequency_min", persisted=True)),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("frequency_min > 0", name="ck_observation_frequencies_min_positive"),
        sa.CheckConstraint("frequency_max > frequency_min", name="ck_observation_frequencies_max_above_min"),
        sa.CheckConstraint("frequency_center > 0", name="ck_observation_frequencies_center_positive"),
    )
    op.create_index("ix_observation_frequencies_observation_active", TABLE, ["observation_id", "is_deleted"])
    op.create_index("ix_observation_frequencies_kind_center", TABLE, ["kind", "frequency_center"])
    op.create_index("ix_observation_frequencies_center", TABLE, ["frequency_center"])


def _check_legacy_rows(bind) -> None:
    """Abort before any change when a legacy set cannot become a valid band"""
    problems = []
    for kind, (low, high, center, _) in LEGACY_SETS.items():
        rows = bind.execute(sa.text(
            f"SELECT id FROM observations WHERE "
            f"(({low} IS NULL) <> ({high} IS NULL)) "
            f"OR ({low} IS NULL AND {center} IS NOT NULL) "
            f"OR ({low} IS NOT NULL AND ({low} <= 0 OR {high} <= {low} OR {center} <= 0)) "
            f"ORDER BY id"
        )).scalars().all()
        if rows:
            problems.append(f"{kind}: observations {rows[:50]}{' ...' if len(rows) > 50 else ''} ({len(rows)} rows)")
    if problems:
        raise RuntimeError(
            "[012_observation_frequencies] frequency values that cannot become a band "
            "(need frequency_min > 0, frequency_max > frequency_min, center > 0, and a center only with its min/max pair). "
            "Fix or clear them, then run the migration again. " + "; ".join(problems)
        )


def _copy_legacy_rows(bind) -> None:
    for kind, (low, high, center, _) in LEGACY_SETS.items():
        other_low = LEGACY_SETS["video" if kind == "rc" else "rc"][0]
        result = bind.execute(sa.text(
            f"INSERT INTO {TABLE} "
            f"(observation_id, kind, link_group, frequency_min, frequency_max, frequency_center, is_deleted, created_at, updated_at) "
            f"SELECT o.id, :kind, CASE WHEN o.{other_low} IS NOT NULL THEN 1 ELSE NULL END, "
            f"o.{low}, o.{high}, COALESCE(o.{center}, (o.{low} + o.{high}) / 2.0), :not_deleted, o.created_at, o.updated_at "
            f"FROM observations o "
            f"WHERE o.{low} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {TABLE} f WHERE f.observation_id = o.id AND f.kind = :kind)"
        ), {"kind": kind, "not_deleted": False})
        _log(f"copied {result.rowcount} {kind} bands")


def _drop_legacy_columns(bind) -> None:
    inspector = sa.inspect(bind)
    present = {c["name"] for c in inspector.get_columns("observations")}
    indexes = [
        idx["name"] for idx in inspector.get_indexes("observations")
        if idx.get("name") and set(idx.get("column_names") or []) & set(LEGACY_COLUMNS)
    ]
    with op.batch_alter_table("observations") as batch:
        for name in indexes:
            _log(f"dropping index {name}")
            batch.drop_index(name)
        for name in LEGACY_COLUMNS:
            if name in present:
                batch.drop_column(name)
    _log("dropped the frequency columns of observations")


def upgrade() -> None:
    bind = op.get_bind()
    _create_table(bind)

    present = {c["name"] for c in sa.inspect(bind).get_columns("observations")}
    if not present & set(LEGACY_COLUMNS):
        return  # fresh install, or already migrated

    missing = set(LEGACY_COLUMNS) - present
    if missing:
        raise RuntimeError(f"[012_observation_frequencies] observations has only part of the frequency columns, missing {sorted(missing)}")

    _check_legacy_rows(bind)
    _copy_legacy_rows(bind)
    _drop_legacy_columns(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = {c["name"] for c in inspector.get_columns("observations")}

    with op.batch_alter_table("observations") as batch:
        for name in LEGACY_COLUMNS:
            if name not in present:
                batch.add_column(sa.Column(name, sa.Numeric(12, 3), nullable=True))
    existing_indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("observations")}
    for name in LEGACY_INDEXED:
        if f"ix_observations_{name}" not in existing_indexes:
            op.create_index(f"ix_observations_{name}", "observations", [name])

    if TABLE not in inspector.get_table_names():
        return

    for kind, (low, high, center, width) in LEGACY_SETS.items():
        first_band = (
            f"(SELECT f.{{col}} FROM {TABLE} f WHERE f.observation_id = observations.id "
            f"AND f.kind = '{kind}' AND f.is_deleted = :not_deleted ORDER BY f.id LIMIT 1)"
        )
        assignments = ", ".join(
            f"{target} = {first_band.format(col=source)}"
            for target, source in ((low, "frequency_min"), (high, "frequency_max"), (center, "frequency_center"), (width, "bandwidth"))
        )
        bind.execute(sa.text(f"UPDATE observations SET {assignments}"), {"not_deleted": False})
    extra = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM {TABLE} f WHERE f.is_deleted = :deleted OR f.id NOT IN "
        f"(SELECT MIN(g.id) FROM {TABLE} g WHERE g.is_deleted = :not_deleted GROUP BY g.observation_id, g.kind)"
    ), {"deleted": True, "not_deleted": False}).scalar()
    _log(f"restored the first active band of each kind; {extra} other bands are dropped with the table")

    # Indexes go with the table (MySQL refuses to drop the one backing the foreign key on its own)
    op.drop_table(TABLE)
