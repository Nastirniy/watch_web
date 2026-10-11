"""022_builtin_roles: roles.builtin_key; the admin role becomes built-in, custom roles named like a key are renamed."""
import sqlite3

import pytest

from tests.test_migrations import run_migrate, HEAD
from tests.test_migration_010_archive_permissions import _rewind

INSERT = "insert into roles (id, name, display_name, created_at, updated_at, is_deleted) values (?, ?, ?, '2026-01-01', '2026-01-01', 0)"


@pytest.mark.slow
def test_custom_role_named_like_a_builtin_is_renamed(tmp_path):
    db_file = tmp_path / "builtin.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.executemany(INSERT, [(1, "admin", "Admin"), (2, "operator", "Custom operator"), (3, "operator_custom", "Taken"),
                              (4, "viewer", "Custom viewer"), (5, "ops", "Ops")])
    _rewind(conn, "021_scope_groups")
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    conn = sqlite3.connect(db_file)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("select id, name, builtin_key from roles").fetchall()}
    assert rows == {
        1: ("admin", "admin"),
        2: ("operator_custom_2", None),
        3: ("operator_custom", None),
        4: ("viewer_custom", None),
        5: ("ops", None),
    }
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()

    third = run_migrate(db_file)
    assert third.returncode == 0 and "renaming custom role" not in third.stdout + third.stderr
    conn = sqlite3.connect(db_file)
    assert {r[0]: (r[1], r[2]) for r in conn.execute("select id, name, builtin_key from roles").fetchall()} == rows
    conn.close()
