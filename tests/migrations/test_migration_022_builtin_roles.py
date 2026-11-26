"""022_builtin_roles: roles.builtin_key; the admin role becomes built-in, custom roles named like a key are renamed."""
import json
import sqlite3

import pytest

from tests.migrations.test_migrations import run_migrate, HEAD
from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migration_010_archive_permissions import _rewind

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


@pytest.mark.slow
def test_case_variants_are_renamed_recorded_and_restored_exactly(tmp_path):
    """MySQL and MariaDB compare role names without case (utf8mb4_unicode_ci; the unique index too), so a legacy
    "Viewer" blocks the built-in "viewer": it is renamed, the log keeps its stored name and the downgrade gives that
    name back. A free suffix is chosen without case as well ("OPERATOR_custom" takes "operator_custom")."""
    db_file = tmp_path / "case.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.executemany(INSERT, [(1, "admin", "Admin"), (2, "Viewer", "Mixed-case viewer"),
                              (3, "OPERATOR_custom", "Taken without case"), (4, "operator", "Custom operator")])
    _rewind(conn, "021_scope_groups")
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "renaming custom role Viewer (id 2) -> viewer_custom" in second.stdout + second.stderr
    conn = sqlite3.connect(db_file)
    names = {role_id: name for role_id, name in conn.execute("select id, name from roles")}
    assert names == {1: "admin", 2: "viewer_custom", 3: "OPERATOR_custom", 4: "operator_custom_2"}
    renames = {ref_id: json.loads(data) for ref_id, data in conn.execute(
        "select ref_id, data from builtin_roles_migration_log where kind = 'renamed_role'")}
    assert renames == {2: {"name": "Viewer", "renamed_to": "viewer_custom"},
                       4: {"name": "operator", "renamed_to": "operator_custom_2"}}
    conn.close()

    down = _run_downgrade(db_file, "021_scope_groups")
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert {role_id: name for role_id, name in conn.execute("select id, name from roles")} == {
        1: "admin", 2: "Viewer", 3: "OPERATOR_custom", 4: "operator"}
    conn.close()
