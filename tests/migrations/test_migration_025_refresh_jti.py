"""025_session_refresh_jti: user_sessions.refresh_jti, the id of the one refresh token that may refresh a session.

A fresh install gets the column from 002_sync_schema (the models), so 025 adds it only where it is missing. A session
from before the upgrade keeps working: its refresh_jti is empty and its first refresh sets it (tests/auth/test_sessions.py).
"""
import sqlite3
from pathlib import Path

from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migrations import HEAD, run_migrate

REVISION = "025_session_refresh_jti"
PREVIOUS = "024_user_certificates"


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


def test_fresh_install_down_and_up_again(tmp_path):
    db_file = tmp_path / "sessions.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert version(db_file) == HEAD and "refresh_jti" in session_columns(db_file)

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert "refresh_jti" not in session_columns(db_file)

    # a session opened before the upgrade
    conn = sqlite3.connect(db_file)
    conn.execute("insert into users (id, username, username_alias, is_active, is_deleted, created_at, updated_at, "
                 "user_type) values (7, 'before', 'Before', 1, 0, '2026-10-19 10:00:00', '2026-10-19 10:00:00', "
                 "'viewer')")
    conn.execute("insert into user_sessions (id, user_id, last_activity, expires_at, created_at) values "
                 "('s-before', 7, '2026-10-19 10:00:00', '2026-10-19 10:30:00', '2026-10-19 10:00:00')")
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert version(db_file) == HEAD
    conn = sqlite3.connect(db_file)
    assert conn.execute("select refresh_jti from user_sessions where id = 's-before'").fetchone() == (None,)
    conn.close()
