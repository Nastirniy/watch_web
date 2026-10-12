"""023_authorization_v2: legacy roles, home posts and access rows become role assignments and user types; the log
and the *_v1 tables give an exact downgrade; a fresh install never has user_roles.

The legacy data is written into a database downgraded to 022_builtin_roles, the shape production has before the
upgrade (downgrading a fresh install recreates user_roles, controller ruling R5)."""
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Dict, List

import pytest

from app.backend.migrations.support.authz_v2 import _DISPLAY_SUFFIX, _VIEW_PART
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

PREVIOUS = "022_builtin_roles"
STAMP = "2026-01-01 00:00:00"

PERMISSIONS = {
    1: "admin.true", 2: "post.access_all", 3: "observation.view", 4: "observation.create",
    5: "observation.update", 6: "post.view", 7: "user.view", 8: "media.view", 9: "audit.view",
}
ROLES = {   # id: (name, builtin_key, permission ids)
    1: ("admin", "admin", list(PERMISSIONS)),
    2: ("ops", None, [3, 4, 5, 6]),
    3: ("mgr", None, [1, 7, 3, 8]),
    4: ("sup", None, [2, 3]),
}
POSTS = (7, 8, 9)
USERS = {   # id: (username, home post, role ids, access rows (post, can_write))
    1: ("chief", None, [1], []),
    2: ("oper", 7, [2], [(8, 1), (9, 0)]),
    3: ("manager", 7, [3], []),
    4: ("super", None, [4], []),
    5: ("nobody", 8, [], []),
}
LEGACY_TABLES = ("permissions", "roles", "role_permissions", "users", "user_roles", "user_post_access")


def legacy_database(tmp_path: Path) -> Path:
    """A database at 022 with the legacy data above (fresh install, downgraded, then filled)"""
    db_file = tmp_path / "authz.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr

    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS
    conn.executemany("insert into permissions (id, name, display_name, description) values (?, ?, ?, '')",
                     [(pid, name, name.upper()) for pid, name in PERMISSIONS.items()])
    for role_id, (name, key, permission_ids) in ROLES.items():
        conn.execute("insert into roles (id, name, display_name, description, created_at, updated_at, is_deleted,"
                     " builtin_key) values (?, ?, ?, null, ?, ?, 0, ?)", (role_id, name, name.title(), STAMP, STAMP, key))
        conn.executemany("insert into role_permissions (role_id, permission_id) values (?, ?)",
                         [(role_id, pid) for pid in permission_ids])
    for post_id in POSTS:
        conn.execute("insert into posts (id, name, code, is_active, is_deleted, created_at, updated_at)"
                     " values (?, ?, ?, 1, 0, ?, ?)", (post_id, f"Post {post_id}", f"P{post_id}", STAMP, STAMP))
    for user_id, (username, home, role_ids, rows) in USERS.items():
        conn.execute(
            "insert into users (id, pki_certificate_cn_hash, pki_certificate_serial_hash, pki_certificate_fingerprint_hash,"
            " username, username_alias, is_active, post_id, is_deleted, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)",
            (user_id, f"cn{user_id}", f"sn{user_id}", f"fp{user_id}", username, username.title(), home, STAMP, STAMP))
        conn.executemany("insert into user_roles (user_id, role_id, assigned_at) values (?, ?, ?)",
                         [(user_id, role_id, STAMP) for role_id in role_ids])
        conn.executemany("insert into user_post_access (user_id, post_id, can_write, created_at) values (?, ?, ?, ?)",
                         [(user_id, post_id, can_write, STAMP) for post_id, can_write in rows])
    conn.commit()
    conn.close()
    return db_file


def snapshot(db_file: Path) -> Dict[str, List[dict]]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = {table: [dict(row) for row in conn.execute(f"select * from {table} order by id")] for table in LEGACY_TABLES}
    conn.close()
    return rows


def tables(conn) -> set:
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def assignments(conn) -> set:
    return set(conn.execute(
        "select a.user_id, r.name, a.scope_key, a.scope_group_id, a.scope_post_id, a.created_by"
        " from role_assignments a join roles r on r.id = a.role_id").fetchall())


@pytest.mark.slow
def test_conversion_writes_assignments_types_and_log(tmp_path):
    db_file = legacy_database(tmp_path)
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr

    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    assert dict(conn.execute("select id, user_type from users")) == {
        1: "admin", 2: "operator", 3: "admin", 4: "analyst", 5: "viewer"}
    assert assignments(conn) == {
        (1, "admin", "root", None, None, None),
        (2, "ops", "p:7", None, 7, None), (2, "ops", "p:8", None, 8, None), (2, "ops_view", "p:9", None, 9, None),
        (3, "mgr_system", "root", None, None, None), (3, "mgr_posts", "p:7", None, 7, None),
        (4, "sup", "root", None, None, None),
    }

    def held(role_name):
        return {name for (name,) in conn.execute(
            "select p.name from permissions p join role_permissions rp on rp.permission_id = p.id"
            " join roles r on r.id = rp.role_id where r.name = ?", (role_name,))}

    assert held("ops_view") == {"observation.view", "post.view"}
    assert held("mgr_system") == {"user.view", "media.monitor"}   # admin.true + media.view: storage monitoring
    assert held("mgr_posts") == {"observation.view", "media.view"}
    assert held("mgr") == {"user.view", "observation.view", "media.view"}   # admin.true left the custom role
    assert conn.execute("select display_name from roles where name = 'ops_view'").fetchone()[0] == \
        "Ops" + _DISPLAY_SUFFIX[_VIEW_PART]

    names = {name for (name,) in conn.execute("select name from permissions")}
    assert not names & {"admin.true", "post.access_all"} and "media.monitor" in names
    assert conn.execute("select count(*) from role_permissions where permission_id in (1, 2)").fetchone()[0] == 0
    assert {"user_roles_v1", "user_post_access_v1", "role_assignments", "authz_v2_migration_log"} <= tables(conn)
    assert not {"user_roles", "user_post_access"} & tables(conn)
    assert conn.execute("select count(*) from user_roles_v1").fetchone()[0] == 4
    assert conn.execute("select count(*) from user_post_access_v1").fetchone()[0] == 2
    assert {row[1]: row[3] for row in conn.execute("pragma table_info(users)")}["user_type"] == 1, "NOT NULL"

    audit = {user_id: json.loads(details) for user_id, details in conn.execute(
        "select resource_id, details from audit_logs where action = 'migrate' and resource_type = 'user'")}
    assert set(audit) == set(USERS)
    assert audit[2]["type"] == "operator"
    assert audit[2]["after"] == [{"role": "ops", "derived": False, "scope": "p:7"},
                                 {"role": "ops", "derived": False, "scope": "p:8"},
                                 {"role": "ops_view", "derived": True, "scope": "p:9"}]
    assert audit[2]["before"]["access"] == [{"post_id": 8, "can_write": True}, {"post_id": 9, "can_write": False}]
    assert audit[3]["differences"] == ["archived_records_need_archive"]
    assert audit[5]["after"] == [] and audit[5]["differences"] == ["no_permissions"]
    kinds = {kind for (kind,) in conn.execute("select kind from authz_v2_migration_log")}
    assert {"removed_permission", "removed_role_permission", "derived_role", "added_permission", "converted"} <= kinds
    assert conn.execute("select count(*) from authz_v2_migration_log where kind = 'converted'").fetchone()[0] == 1
    conn.close()

    # 023 really runs again (stamped back to 022): the converted database is recognised and left as it is
    rerun = rerun_023(db_file)
    assert "already converted" in rerun and "converting" not in rerun, rerun
    conn = sqlite3.connect(db_file)
    assert len(assignments(conn)) == 7
    assert migrate_audit_rows(conn) == len(USERS)
    conn.close()


def rerun_023(db_file: Path) -> str:
    """Stamps the database back to 022 and migrates to head again: 023 runs once more; its output"""
    conn = sqlite3.connect(db_file)
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()
    return result.stdout + result.stderr


def migrate_audit_rows(conn) -> int:
    return conn.execute("select count(*) from audit_logs where action = 'migrate'").fetchone()[0]


@pytest.mark.slow
def test_a_rerun_finishes_the_renames_without_converting_again(tmp_path):
    """MySQL commits the conversion with the first RENAME. A rename that failed (no *_v1 at all) or a crash between
    the two renames leaves converted data under legacy table names: the converted marker in the log, not the *_v1
    tables, tells a rerun that the data is converted, and each legacy table still under its old name is renamed"""
    db_file = legacy_database(tmp_path)
    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    converted = assignments(conn)
    assert len(converted) == 7 and migrate_audit_rows(conn) == len(USERS)
    conn.close()

    for unrenamed in (("user_roles", "user_post_access"), ("user_post_access",)):
        conn = sqlite3.connect(db_file)
        for legacy in unrenamed:
            conn.execute(f"alter table {legacy}_v1 rename to {legacy}")
        conn.commit()
        conn.close()

        rerun = rerun_023(db_file)
        assert "already converted" in rerun and "converting" not in rerun, rerun
        conn = sqlite3.connect(db_file)
        present = tables(conn)
        assert {"user_roles_v1", "user_post_access_v1"} <= present, (unrenamed, present)
        assert not {"user_roles", "user_post_access"} & present, (unrenamed, present)
        assert conn.execute("select count(*) from user_roles_v1").fetchone()[0] == 4
        assert conn.execute("select count(*) from user_post_access_v1").fetchone()[0] == 2
        assert assignments(conn) == converted and migrate_audit_rows(conn) == len(USERS)
        conn.close()

    # and the downgrade still restores from the *_v1 tables and the log (the marker goes with the log)
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert {"user_roles", "user_post_access"} <= tables(conn)
    assert not {"authz_v2_migration_log", "user_roles_v1", "user_post_access_v1"} & tables(conn)
    conn.close()


@pytest.mark.slow
def test_downgrade_restores_exactly(tmp_path):
    db_file = legacy_database(tmp_path)
    before = snapshot(db_file)
    assert run_migrate(db_file).returncode == 0

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert snapshot(db_file) == before, "legacy rows, removed permissions and grants back, derived roles gone"
    conn = sqlite3.connect(db_file)
    assert not {"role_assignments", "authz_v2_migration_log", "user_roles_v1", "user_post_access_v1"} & tables(conn)
    assert "user_type" not in {row[1] for row in conn.execute("pragma table_info(users)")}
    conn.close()

    # and up again: the same conversion
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert len(assignments(conn)) == 7
    conn.close()


@pytest.mark.slow
def test_fresh_install_chain(tmp_path):
    db_file = tmp_path / "fresh.db"
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    present = tables(conn)
    assert "role_assignments" in present and "user_roles" not in present and "user_post_access" not in present
    assert "user_type" in {row[1] for row in conn.execute("pragma table_info(users)")}
    unique = [row[1] for row in conn.execute("pragma index_list(role_assignments)") if row[2] == 1]
    assert any(set(c[2] for c in conn.execute(f"pragma index_info({name})")) == {"user_id", "role_id", "scope_key"}
               for name in unique)
    # the CHECK: never a group and a post at once
    conn.execute("insert into users (id, pki_certificate_cn_hash, pki_certificate_serial_hash,"
                 " pki_certificate_fingerprint_hash, username, username_alias, is_active, is_deleted, created_at,"
                 " updated_at, user_type) values (1, 'c', 's', 'f', 'u', 'U', 1, 0, ?, ?, 'viewer')", (STAMP, STAMP))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into role_assignments (user_id, role_id, scope_group_id, scope_post_id, scope_key,"
                     " created_at) values (1, 1, 1, 1, 'g:1', ?)", (STAMP,))
    conn.rollback()
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()

    # 023 again on a converted database: nothing to convert, nothing renamed
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already converted" in again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()


def _load_023():
    path = PROJECT_ROOT / "app" / "backend" / "migrations" / "versions" / "023_authorization_v2.py"
    spec = importlib.util.spec_from_file_location("migration_023", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_conversion_writes_only_root_and_post_nodes():
    """The frozen plan emits "root" and "p:<id>" only; anything else stops the migration instead of becoming an
    assignment with both scope ids NULL and a scope_key that is not the root"""
    scope_post_id = _load_023().scope_post_id
    assert scope_post_id("root") is None
    assert scope_post_id("p:7") == 7
    for node in ("g:3", "p:", "p:x", "p:-1", "post:7", "", "Root"):
        with pytest.raises(ValueError):
            scope_post_id(node)
