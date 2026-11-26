"""026_users_no_hot_columns: users.last_login_at and users.last_record_created_at are gone (tests/infra/test_hot_rows.py).

An upgraded database loses both columns; last_login_at is read from the sessions from then on. The downgrade adds them
back and fills last_login_at from the sessions, so the previous code finds what it expects.
"""
import sqlite3
from pathlib import Path

from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migrations import run_migrate

REVISION = "026_users_no_hot_columns"
PREVIOUS = "025_session_refresh_jti"


def user_columns(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute("pragma table_info(users)")}
    conn.close()
    return names


def test_down_fills_last_login_and_up_drops_both(tmp_path):
    db_file = tmp_path / "users.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert not user_columns(db_file) & {"last_login_at", "last_record_created_at"}

    conn = sqlite3.connect(db_file)
    conn.execute("insert into users (id, username, username_alias, is_active, is_deleted, created_at, updated_at, "
                 "user_type) values (7, 'logged', 'Logged', 1, 0, '2026-10-19 10:00:00', '2026-10-19 10:00:00', "
                 "'viewer')")
    for sid, created in (("s-1", "2026-10-19 10:00:00"), ("s-2", "2026-10-19 12:30:00")):
        conn.execute("insert into user_sessions (id, user_id, last_activity, expires_at, created_at) values "
                     "(?, 7, ?, ?, ?)", (sid, created, created, created))
    conn.commit()
    conn.close()

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert {"last_login_at", "last_record_created_at"} <= user_columns(db_file)
    conn = sqlite3.connect(db_file)
    assert conn.execute("select last_login_at from users where id = 7").fetchone()[0].startswith("2026-10-19 12:30:00")
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert not user_columns(db_file) & {"last_login_at", "last_record_created_at"}
    conn = sqlite3.connect(db_file)
    assert conn.execute("select username from users where id = 7").fetchone() == ("logged",)
    conn.close()
