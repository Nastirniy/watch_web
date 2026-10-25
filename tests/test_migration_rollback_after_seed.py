"""A downgrade to 020 after the application started (seeding ran) restores the legacy tables exactly (controller
ruling R31, spec section 8): 023 replays its log, 022 deletes the built-in roles seeding created, renames the roles it
renamed back, restores the texts seeding refreshed and deletes media.monitor, 021 deletes scope_group.*. A second
upgrade, seeding and downgrade leaves no orphan role.

The legacy database is a fresh install downgraded to 020 (as 023's tests do at 022) filled with the catalog of the
code before authorization v2: every permission of today but scope_group.* and media.monitor, plus admin.true and
post.access_all."""
import asyncio
import sqlite3
from pathlib import Path
from typing import Dict, List

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.general import Permissions
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, run_migrate

LEGACY_REVISION = "020_post_archive"
STAMP = "2026-01-01 00:00:00"
ADDED_BY_V2 = {"scope_group.view", "scope_group.create", "scope_group.update", "scope_group.archive", "media.monitor"}
LEGACY_PERMISSIONS = sorted(({p.value for p in Permissions} - ADDED_BY_V2) | {"admin.true", "post.access_all"})
PERMISSION_IDS = {name: index for index, name in enumerate(LEGACY_PERMISSIONS, start=1)}
ROLES = {   # id: (name, display name, is_deleted, permissions)
    1: ("admin", "Administrator", 0, LEGACY_PERMISSIONS),
    2: ("viewer", "Custom viewer", 0, ["post.view", "observation.view"]),
    3: ("operator", "Custom operator", 0, ["post.view", "observation.view", "observation.create"]),
    4: ("operator_custom", "Taken name", 0, ["post.view"]),
    5: ("chief", "Chief", 0, ["admin.true", "post.view", "user.view", "media.view"]),
    6: ("old", "Old", 1, ["observation.view", "tag.create"]),
}
POSTS = (7, 8)
USERS = {   # id: (username, home post, role ids, access rows (post, can_write))
    1: ("boss", None, [1], []),
    2: ("oper", 7, [3], [(8, 0)]),
    3: ("watcher", 8, [2], []),
    4: ("manager", 7, [5], []),
    5: ("taken", 8, [4, 6], []),
}
TABLES = ("permissions", "roles", "role_permissions", "users", "user_roles", "user_post_access")


def legacy_020(tmp_path: Path) -> Path:
    db_file = tmp_path / "rollback.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, LEGACY_REVISION)
    assert down.returncode == 0, down.stdout + down.stderr

    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == LEGACY_REVISION
    conn.executemany("insert into permissions (id, name, display_name, description) values (?, ?, ?, ?)",
                     [(pid, name, f"Legacy {name}", f"legacy text of {name}") for name, pid in PERMISSION_IDS.items()])
    for role_id, (name, display_name, deleted, permissions) in ROLES.items():
        conn.execute("insert into roles (id, name, display_name, description, created_at, updated_at, is_deleted)"
                     " values (?, ?, ?, ?, ?, ?, ?)", (role_id, name, display_name, f"legacy {name}", STAMP, STAMP, deleted))
        conn.executemany("insert into role_permissions (role_id, permission_id) values (?, ?)",
                         [(role_id, PERMISSION_IDS[permission]) for permission in permissions])
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
    rows = {table: [dict(row) for row in conn.execute(f"select * from {table} order by id")] for table in TABLES}
    conn.close()
    return rows


def seed(db_file: Path) -> None:
    """What the application does at start after the migrations: permissions and the built-in roles"""
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_roles_and_permissions(session)
        await engine.dispose()
    asyncio.run(run())


def tables(conn) -> set:
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def upgrade_and_seed(db_file: Path) -> None:
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    seed(db_file)
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    roles = {name: key for name, key in conn.execute("select name, builtin_key from roles")}
    # seeding ran: every built-in role exists, the custom roles carry the names 022 gave them, texts refreshed
    assert {key for key in roles.values() if key} == {"viewer", "operator", "senior_operator", "analyst",
                                                       "post_admin", "admin"}
    assert roles["viewer_custom"] is None and roles["operator_custom_2"] is None and roles["operator_custom"] is None
    assert conn.execute("select display_name from roles where id = 1").fetchone()[0] != "Administrator"
    assert conn.execute("select display_name from permissions where name = 'post.view'").fetchone()[0] != "Legacy post.view"
    assert ADDED_BY_V2 <= {name for (name,) in conn.execute("select name from permissions")}
    conn.close()


@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="authorization v3: seeding at 029 without the conversion of 030 (plan Task 8) cannot be downgraded exactly")
def test_downgrade_after_seeding_restores_the_legacy_tables(tmp_path):
    db_file = legacy_020(tmp_path)
    before = snapshot(db_file)

    for cycle in (1, 2):   # the second cycle: no orphan role, nothing accumulates
        upgrade_and_seed(db_file)
        down = _run_downgrade(db_file, LEGACY_REVISION)
        assert down.returncode == 0, down.stdout + down.stderr
        after = snapshot(db_file)
        for table in TABLES:
            assert after[table] == before[table], (cycle, table)
        conn = sqlite3.connect(db_file)
        assert conn.execute("select version_num from alembic_version").fetchone()[0] == LEGACY_REVISION
        assert not {"builtin_roles_migration_log", "scope_groups", "role_assignments"} & tables(conn)
        assert "builtin_key" not in {row[1] for row in conn.execute("pragma table_info(roles)")}
        conn.close()


@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="authorization v3: seeding at 029 without the conversion of 030 (plan Task 8) cannot be downgraded exactly")
def test_a_seeded_role_with_legacy_holders_is_kept(tmp_path):
    """A built-in role a legacy user_roles row refers to is not deleted (a line says so), and the custom role 022
    renamed away from that name keeps its new name (a line says so) instead of colliding"""
    db_file = legacy_020(tmp_path)
    upgrade_and_seed(db_file)
    conn = sqlite3.connect(db_file)
    seeded_viewer = conn.execute("select id from roles where builtin_key = 'viewer'").fetchone()[0]
    conn.execute("insert into user_roles_v1 (user_id, role_id, assigned_at) values (3, ?, ?)", (seeded_viewer, STAMP))
    conn.commit()
    conn.close()

    down = _run_downgrade(db_file, LEGACY_REVISION)
    assert down.returncode == 0, down.stdout + down.stderr
    output = down.stdout + down.stderr
    assert f"keeping built-in role viewer (id {seeded_viewer})" in output, output
    assert "keeping the name viewer_custom of role 2" in output, output
    conn = sqlite3.connect(db_file)
    names = {role_id: name for role_id, name in conn.execute("select id, name from roles")}
    assert names[seeded_viewer] == "viewer" and names[2] == "viewer_custom" and names[3] == "operator"
    assert not {"senior_operator", "analyst", "post_admin"} & set(names.values())
    conn.close()
