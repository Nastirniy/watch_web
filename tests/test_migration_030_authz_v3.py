"""030_authorization_v3: v2 data (one role kind, admin / post_admin types) becomes tree roles, system roles and
system assignments; every user keeps every permission at every node (former post_admin holders gain unit
management); the log gives an exact downgrade to 029; a second run is a no-op.

The v2 data is written into a database downgraded to 029_authz_v3_schema (the additive revision), the shape a
production database has right before 030."""
import json
import sqlite3
from pathlib import Path
from typing import Dict, FrozenSet, List

import pytest

from app.backend.migrations.support.authz_v3 import (
    ALLOWED_GAIN, LEVELS, SYSTEM_BOUNDARY, TREE_BOUNDARY, V2Assignment, V2Role, V2User, effective,
)
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, run_migrate

PREVIOUS = "029_authz_v3_schema"
STAMP = "2026-01-01 00:00:00"
NAMES = ["post.view", "observation.view", "observation.create", "user.view", "user.create", "scope_group.view",
         "scope_group.create", "scope_group.update", "scope_group.archive", "audit.view", "tag.create",
         "database.view", "role.create"]
PERMISSIONS = {i + 1: name for i, name in enumerate(NAMES)}
BY_NAME = {name: pid for pid, name in PERMISSIONS.items()}
ADMIN_ALL = NAMES
# As seeded at 028: every tree permission but unit management
POST_ADMIN = [n for n in NAMES if LEVELS[n] == "tree" and n not in ALLOWED_GAIN]
ROLES = {   # id: (name, builtin_key, archived, permission names)
    1: ("admin", "admin", 0, ADMIN_ALL),
    2: ("post_admin", "post_admin", 0, POST_ADMIN),
    3: ("operator", "operator", 0, ["post.view", "observation.view", "observation.create"]),
    4: ("ops_system", None, 0, ["post.view", "audit.view"]),      # a v2 derived root role mixing both levels
    5: ("aud", None, 0, ["audit.view"]),
    6: ("Auditor", None, 0, ["post.view"]),                       # named like a new built-in role
    7: ("old", None, 1, ["post.view", "tag.create"]),
    8: ("administrator", None, 0, ["post.view"]),                 # named like the renamed built-in post_admin
}
USERS = {   # id: (username, v2 type, post)
    1: ("chief", "admin", None), 2: ("brigade", "post_admin", None), 3: ("oper", "operator", 7),
    4: ("manager", "admin", None), 5: ("reader", "viewer", 7), 6: ("keeper", "admin", None),
}
ASSIGNMENTS = {   # id: (user, role, scope key)
    1: (1, 1, "root"), 2: (2, 2, "g:3"), 3: (3, 3, "p:7"), 4: (4, 4, "root"), 5: (4, 5, "root"),
    6: (5, 6, "p:7"), 7: (6, 7, "root"), 8: (6, 3, "root"), 9: (5, 8, "p:7"),
}
TABLES = ("permissions", "roles", "role_permissions", "role_assignments", "users", "system_role_assignments")


def v2_database(tmp_path: Path) -> Path:
    db_file = tmp_path / "authz3.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS
    conn.executemany("insert into permissions (id, name, display_name, description) values (?, ?, ?, '')",
                     [(pid, name, name.upper()) for pid, name in PERMISSIONS.items()])
    for role_id, (name, key, archived, names) in ROLES.items():
        conn.execute("insert into roles (id, name, display_name, description, created_at, updated_at, is_deleted,"
                     " builtin_key) values (?, ?, ?, null, ?, ?, ?, ?)",
                     (role_id, name, name.title(), STAMP, STAMP, archived, key))
        conn.executemany("insert into role_permissions (role_id, permission_id) values (?, ?)",
                         [(role_id, BY_NAME[n]) for n in names])
    conn.execute("insert into scope_groups (id, name, parent_key, kind, is_deleted, created_at, updated_at)"
                 " values (3, 'Brigade', 0, 'group', 0, ?, ?)", (STAMP, STAMP))
    conn.execute("insert into posts (id, name, code, is_active, is_deleted, created_at, updated_at, group_id)"
                 " values (7, 'Post 7', 'P7', 1, 0, ?, ?, 3)", (STAMP, STAMP))
    for user_id, (username, user_type, post) in USERS.items():
        conn.execute("insert into users (id, username, username_alias, is_active, post_id, user_type, is_deleted,"
                     " created_at, updated_at) values (?, ?, ?, 1, ?, ?, 0, ?, ?)",
                     (user_id, username, username.title(), post, user_type, STAMP, STAMP))
    for assignment_id, (user_id, role_id, key) in ASSIGNMENTS.items():
        group = int(key[2:]) if key.startswith("g:") else None
        post = int(key[2:]) if key.startswith("p:") else None
        conn.execute("insert into role_assignments (id, user_id, role_id, scope_group_id, scope_post_id, scope_key,"
                     " created_at) values (?, ?, ?, ?, ?, ?, ?)", (assignment_id, user_id, role_id, group, post, key, STAMP))
    conn.commit()
    conn.close()
    return db_file


def snapshot(db_file: Path) -> Dict[str, List[dict]]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = {table: [dict(row) for row in conn.execute(f"select * from {table} order by id")] for table in TABLES}
    conn.close()
    return rows


def v2_model():
    roles = {rid: V2Role(rid, name, name.title(), bool(archived), key, frozenset(names))
             for rid, (name, key, archived, names) in ROLES.items()}
    assignments = [V2Assignment(aid, uid, rid, key) for aid, (uid, rid, key) in ASSIGNMENTS.items()]
    users = [V2User(uid, user_type) for uid, (_, user_type, _) in USERS.items()]
    return roles, assignments, users


def effective_after(db_file: Path, user_id: int) -> Dict[str, FrozenSet[str]]:
    """What the user has in effect per scope key, read from the converted database with the v3 rules"""
    conn = sqlite3.connect(db_file)
    (user_type,) = conn.execute("select user_type from users where id = ?", (user_id,)).fetchone()
    rows = conn.execute(
        "select 'tree', a.scope_key, r.kind, p.name from role_assignments a join roles r on r.id = a.role_id"
        " join role_permissions rp on rp.role_id = r.id join permissions p on p.id = rp.permission_id"
        " where a.user_id = ? and r.is_deleted = 0"
        " union all select 'system', 'root', r.kind, p.name from system_role_assignments s"
        " join roles r on r.id = s.role_id join role_permissions rp on rp.role_id = r.id"
        " join permissions p on p.id = rp.permission_id where s.user_id = ? and r.is_deleted = 0",
        (user_id, user_id)).fetchall()
    conn.close()
    held: Dict[str, set] = {}
    for table, key, kind, name in rows:
        assert kind == table, (user_id, key, name)
        bound = (TREE_BOUNDARY if table == "tree" else SYSTEM_BOUNDARY)[user_type]
        if LEVELS[name] == table and name in bound:
            held.setdefault(key, set()).add(name)
    return {key: frozenset(names) for key, names in held.items()}


@pytest.mark.slow
def test_effective_permissions_equal_except_allowed_gain(tmp_path):
    db_file = v2_database(tmp_path)
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    roles, assignments, users = v2_model()
    for user_id in USERS:
        before = effective(None, roles, assignments, users, user_id)
        after = effective_after(db_file, user_id)
        assert set(before) == set(after), user_id
        for key in before:
            assert before[key] <= after[key], (user_id, key)
            assert after[key] - before[key] <= ALLOWED_GAIN, (user_id, key)
    assert effective_after(db_file, 2)["g:3"] - effective(None, roles, assignments, users, 2)["g:3"] == ALLOWED_GAIN


@pytest.mark.slow
def test_conversion_rows(tmp_path):
    """Review Focus 5 included: the mixed v2 derived role ops_system is split"""
    db_file = v2_database(tmp_path)
    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    roles = {name: (rid, key, kind) for rid, name, key, kind in conn.execute("select id, name, builtin_key, kind from roles")}
    assert roles["administrator"] == (2, "administrator", "tree")
    assert roles["system_administrator"] == (1, "system_administrator", "system")
    assert roles["ops_system"][2] == "tree" and roles["ops_system_system"][2] == "system"
    assert roles["aud"][2] == "system" and roles["Auditor_custom"][0] == 6
    assert roles["administrator_custom"][0] == 8      # renamed before post_admin takes the name (final review C1)
    assert conn.execute("select level from permissions where name = 'audit.view'").fetchone() == ("system",)
    types = dict(conn.execute("select id, user_type from users"))
    assert types == {1: "system_administrator", 2: "administrator", 3: "operator", 4: "system_administrator",
                     5: "viewer", 6: "system_administrator"}
    system = set(conn.execute("select s.user_id, r.name from system_role_assignments s join roles r on r.id = s.role_id"))
    assert system == {(1, "system_administrator"), (4, "ops_system_system"), (4, "aud"), (6, "old_system")}
    assert conn.execute("select is_deleted from roles where name = 'old_system'").fetchone() == (1,)
    tree = set(conn.execute("select a.user_id, r.name, a.scope_key from role_assignments a join roles r on r.id = a.role_id"))
    assert (1, "administrator", "root") in tree and (4, "ops_system", "root") in tree
    assert not {row for row in tree if row[1] in ("aud", "system_administrator")}
    audits = conn.execute("select count(*) from audit_logs where action = 'migrate' and resource_type = 'user'").fetchone()[0]
    assert audits == 6
    conn.close()


@pytest.mark.slow
def test_downgrade_exact_and_rerun_noop(tmp_path):
    db_file = v2_database(tmp_path)
    before = snapshot(db_file)
    assert run_migrate(db_file).returncode == 0
    converted = snapshot(db_file)
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    after = snapshot(db_file)
    for table in TABLES:
        assert after[table] == before[table], table

    assert run_migrate(db_file).returncode == 0
    assert snapshot(db_file) == converted
    conn = sqlite3.connect(db_file)
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert snapshot(db_file) == converted
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    log = [json.loads(data) if data else None for (data,) in conn.execute(
        "select data from authz_v3_migration_log where kind = 'converted'")]
    assert len(log) == 1
    conn.close()


@pytest.mark.slow
def test_report_v3_on_the_database_before_the_conversion(tmp_path, capsys):
    from app.backend.cli import authz_report
    db_file = v2_database(tmp_path)
    assert authz_report.main(["--v3", "--json", "--url", f"sqlite:///{db_file}"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["revision"] == PREVIOUS and report["unexpected_differences"] == []
    assert report["system_administrators"] == [1]
    assert {s["name"] for s in report["split_roles"]} == {"ops_system_system", "old_system"}
    assert {u["user_id"]: u["type_after"] for u in report["users"]}[2] == "administrator"
    assert run_migrate(db_file).returncode == 0
    assert authz_report.main(["--v3", "--url", f"sqlite:///{db_file}"]) == 2


@pytest.mark.slow
def test_downgrade_maps_users_made_after_the_upgrade(tmp_path):
    """Final review I4: a user created or retyped after the upgrade keeps a v3 type the v2 code cannot read; the
    downgrade maps it back and gives a former system administrator the v2 admin role at the root"""
    db_file = v2_database(tmp_path)
    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    (administrator,) = conn.execute("select id from roles where builtin_key = 'administrator'").fetchone()
    (system_admin,) = conn.execute("select id from roles where builtin_key = 'system_administrator'").fetchone()
    conn.execute("insert into users (id, username, username_alias, is_active, user_type, is_deleted, created_at,"
                 " updated_at) values (20, 'later', 'Later', 1, 'system_administrator', 0, ?, ?)", (STAMP, STAMP))
    conn.execute("insert into users (id, username, username_alias, is_active, user_type, is_deleted, created_at,"
                 " updated_at) values (21, 'unit', 'Unit', 1, 'administrator', 0, ?, ?)", (STAMP, STAMP))
    conn.execute("insert into role_assignments (user_id, role_id, scope_key, created_at) values (20, ?, 'root', ?)",
                 (administrator, STAMP))
    conn.execute("insert into system_role_assignments (user_id, role_id, created_at) values (20, ?, ?)",
                 (system_admin, STAMP))
    conn.execute("insert into role_assignments (user_id, role_id, scope_group_id, scope_key, created_at)"
                 " values (21, ?, 3, 'g:3', ?)", (administrator, STAMP))
    conn.commit()
    conn.close()
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    types = dict(conn.execute("select id, user_type from users where id in (20, 21)"))
    assert types == {20: "admin", 21: "post_admin"}
    held = set(conn.execute("select a.user_id, r.builtin_key, a.scope_key from role_assignments a"
                            " join roles r on r.id = a.role_id where a.user_id in (20, 21)"))
    assert (20, "admin", "root") in held and (21, "post_admin", "g:3") in held
    conn.close()
