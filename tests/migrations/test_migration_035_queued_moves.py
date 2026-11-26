"""035_queued_moves: the tables pending_moves and move_locks, upload_sessions.last_chunk_at.

A fresh install gets the column from 002_sync_schema (the models) and the tables from 035, which is why they are listed
in TABLES_OWNED_BY_LATER_REVISIONS; every create is guarded.
"""
import sqlite3
from pathlib import Path

from alembic.script import ScriptDirectory

from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migrations import HEAD, PROJECT_ROOT, run_migrate

REVISION = "035_queued_moves"
PREVIOUS = "034_session_refresh_reuse"


def tables(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    conn.close()
    return names


def upload_columns(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute("pragma table_info(upload_sessions)")}
    conn.close()
    return names


def test_035_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert scripts.get_revision(HEAD) is not None


def test_fresh_install_down_and_up_again(tmp_path):
    db_file = tmp_path / "moves.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert {"pending_moves", "move_locks"} <= tables(db_file) and "last_chunk_at" in upload_columns(db_file)
    conn = sqlite3.connect(db_file)
    assert conn.execute("select id from move_locks").fetchall() == [(1,)]
    assert {"ix_pending_moves_waiting", "ix_pending_moves_node"} <= {
        row[1] for row in conn.execute("pragma index_list(pending_moves)")}
    conn.close()

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert not {"pending_moves", "move_locks"} & tables(db_file) and "last_chunk_at" not in upload_columns(db_file)

    # a session that has received bytes starts from its last activity, one that has none from nothing
    conn = sqlite3.connect(db_file)
    conn.execute("insert into users (id, username, username_alias, is_active, is_deleted, created_at, updated_at, "
                 "user_type) values (7, 'before', 'Before', 1, 0, '2026-10-30 10:00:00', '2026-10-30 10:00:00', "
                 "'viewer')")
    for upload_id, received in (("a" * 32, 10), ("b" * 32, 0)):
        conn.execute(
            "insert into upload_sessions (id, user_id, observation_id, post_id, media_type, original_filename, "
            "expected_size, received_bytes, status, part_path, created_at, updated_at, last_activity_at, expires_at) "
            "values (?, 7, 1, 1, 'image', 'a.png', 100, ?, 'open', 'p', '2026-10-30 10:00:00', "
            "'2026-10-30 10:00:00', '2026-10-30 10:05:00', '2026-10-31 10:00:00')", (upload_id, received))
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select id from move_locks").fetchall() == [(1,)], "one row, on every upgrade"
    rows = dict(conn.execute("select id, last_chunk_at from upload_sessions"))
    conn.close()
    assert rows["a" * 32].startswith("2026-10-30 10:05:00") and rows["b" * 32] is None
