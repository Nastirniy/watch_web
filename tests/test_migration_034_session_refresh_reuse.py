"""034_session_refresh_reuse: user_sessions.previous_refresh_jti and refresh_rotated_at, the token the last refresh
retired and when (auth/sessions.py refresh_reuse).

A fresh install gets the columns from 002_sync_schema (the models), so 034 adds them only where they are missing. A
session from before the upgrade keeps both empty: a used token is answered as before until its next refresh
(tests/test_refresh_reuse.py).
"""
import sqlite3
from pathlib import Path

from alembic.script import ScriptDirectory

from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

REVISION = "034_session_refresh_reuse"
PREVIOUS = "033_user_guides"
COLUMNS = {"previous_refresh_jti", "refresh_rotated_at"}


def session_columns(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute("pragma table_info(user_sessions)")}
    conn.close()
    return names


def version(db_file: Path) -> str:
    conn = sqlite3.connect(db_file)
    value = conn.execute("select version_num from alembic_version").fetchone()[0]
    conn.close()
    return value


def test_034_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert scripts.get_revision(HEAD) is not None


def test_fresh_install_down_and_up_again(tmp_path):
    db_file = tmp_path / "sessions.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert version(db_file) == HEAD and COLUMNS <= session_columns(db_file)

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert version(db_file) == PREVIOUS and not COLUMNS & session_columns(db_file)
    assert "refresh_jti" in session_columns(db_file)

    # a session opened before the upgrade
    conn = sqlite3.connect(db_file)
    conn.execute("insert into users (id, username, username_alias, is_active, is_deleted, created_at, updated_at, "
                 "user_type) values (7, 'before', 'Before', 1, 0, '2026-10-30 10:00:00', '2026-10-30 10:00:00', "
                 "'viewer')")
    conn.execute("insert into user_sessions (id, user_id, last_activity, expires_at, created_at, refresh_jti) values "
                 "('s-before', 7, '2026-10-30 10:00:00', '2026-10-30 10:30:00', '2026-10-30 10:00:00', 'jti')")
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert version(db_file) == HEAD
    conn = sqlite3.connect(db_file)
    assert conn.execute("select refresh_jti, previous_refresh_jti, refresh_rotated_at from user_sessions "
                        "where id = 's-before'").fetchone() == ("jti", None, None)
    conn.close()
