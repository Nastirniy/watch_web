"""012_observation_frequencies on a sqlite database that still has the flat frequency columns."""
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.test_migrations import PROJECT_ROOT, run_migrate

LEGACY_COLUMNS = (
    "frequency_min", "frequency_max", "frequency_center", "bandwidth",
    "video_frequency_min", "video_frequency_max", "video_frequency_center", "video_bandwidth",
)


def run_manager(db_file, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WATCH_WEB_ALEMBIC_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    return subprocess.run(
        [sys.executable, "-m", "app.backend.cli.migrations_manager", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


def make_legacy_database(db_file, rows):
    """Head schema rewound to 011: flat columns back on observations, no observation_frequencies"""
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    conn = sqlite3.connect(db_file)
    conn.execute("drop table observation_frequencies")
    for name in LEGACY_COLUMNS:
        conn.execute(f"alter table observations add column {name} numeric(12, 3)")
    conn.execute("create index ix_observations_frequency_center on observations (frequency_center)")
    conn.execute("create index ix_observations_video_frequency_center on observations (video_frequency_center)")
    for observation_id, values in rows:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            "insert into observations (id, user_id, post_id, signal_type_id, observed_at, status, confidence_level,"
            f" is_deleted, created_at, updated_at, {columns})"
            " values (?, 1, 1, 1, '2026-09-01 10:00:00', 'active', 'medium', 0, '2026-09-01 10:00:00', '2026-09-01 10:00:00',"
            f" {placeholders})",
            (observation_id, *values.values()),
        )
    conn.execute("delete from alembic_version")
    conn.execute("insert into alembic_version values ('011_upload_sessions')")
    conn.commit()
    conn.close()


@pytest.mark.slow
def test_012_moves_flat_columns_into_bands_and_back(tmp_path):
    db_file = tmp_path / "legacy_frequencies.db"
    make_legacy_database(db_file, [
        (1, {"frequency_min": 100, "frequency_max": 200, "frequency_center": 150, "bandwidth": 100}),
        (2, {"frequency_min": 2400, "frequency_max": 2480, "video_frequency_min": 5720, "video_frequency_max": 5800,
             "video_frequency_center": 5760}),
        (3, {"video_frequency_min": 1200, "video_frequency_max": 1300}),
    ])

    upgraded = run_migrate(db_file)
    output = upgraded.stdout + upgraded.stderr
    assert upgraded.returncode == 0, output
    assert "copied 2 rc bands" in output and "copied 2 video bands" in output

    conn = sqlite3.connect(db_file)
    bands = conn.execute(
        "select observation_id, kind, link_group, frequency_min, frequency_max, frequency_center, bandwidth, is_deleted"
        " from observation_frequencies order by observation_id, kind"
    ).fetchall()
    assert bands == [
        (1, "rc", None, 100, 200, 150, 100, 0),
        (2, "rc", 1, 2400, 2480, 2440, 80, 0),
        (2, "video", 1, 5720, 5800, 5760, 80, 0),
        (3, "video", None, 1200, 1300, 1250, 100, 0),
    ]
    columns = {row[1] for row in conn.execute("pragma table_info(observations)")}
    assert not columns & set(LEGACY_COLUMNS)
    indexes = {row[1] for row in conn.execute("pragma index_list(observations)")}
    assert "ix_observations_frequency_center" not in indexes
    assert conn.execute("select count(*) from observations").fetchone()[0] == 3
    # a second band of the same kind exists only in the new table
    conn.execute(
        "insert into observation_frequencies (observation_id, kind, frequency_min, frequency_max, frequency_center, is_deleted, created_at, updated_at)"
        " values (1, 'rc', 300, 400, 350, 0, '2026-09-02 00:00:00', '2026-09-02 00:00:00')"
    )
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "copied" not in again.stdout + again.stderr

    downgraded = run_manager(db_file, "downgrade", "011_upload_sessions")
    output = downgraded.stdout + downgraded.stderr
    conn = sqlite3.connect(db_file)
    assert "observation_frequencies" not in {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}, output
    restored = conn.execute(
        "select id, frequency_min, frequency_max, frequency_center, bandwidth, video_frequency_min, video_frequency_max,"
        " video_frequency_center, video_bandwidth from observations order by id"
    ).fetchall()
    assert restored == [
        (1, 100, 200, 150, 100, None, None, None, None),
        (2, 2400, 2480, 2440, 80, 5720, 5800, 5760, 80),
        (3, None, None, None, None, 1200, 1300, 1250, 100),
    ]
    assert "1 other bands are dropped" in output
    conn.close()


@pytest.mark.slow
def test_012_stops_on_values_that_cannot_become_a_band(tmp_path):
    db_file = tmp_path / "broken_frequencies.db"
    make_legacy_database(db_file, [
        (1, {"frequency_min": 100, "frequency_max": 200}),
        (7, {"frequency_center": 150}),
        (8, {"video_frequency_min": 500, "video_frequency_max": 400}),
    ])

    result = run_migrate(db_file)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "cannot become a band" in output
    assert "rc: observations [7]" in output and "video: observations [8]" in output

    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(observations)")}
    assert set(LEGACY_COLUMNS) <= columns, "nothing is dropped when the check fails"
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == "011_upload_sessions"
    conn.close()
