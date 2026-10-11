"""Scope groups: nested groups of posts (/scope-groups), the tree, posts.group_id, migration 021."""
import sqlite3

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import PostRoutes, ScopeGroupRoutes
from app.backend.models.audit import AuditLog
from app.backend.services.scope_groups import MAX_GROUP_DEPTH
from tests.helpers import make_admin, make_post
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, run_migrate

ROOT = V1.path(ScopeGroupRoutes.ROOT)


def _group(group_id: int) -> str:
    return V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=group_id)


async def _create(client, h, name, parent_id=None, expect=200):
    r = await client.post(ROOT, json={"name": name, "parent_id": parent_id}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


async def test_create_nested_groups_and_tree(client, db, headers):
    h = headers(await make_admin(db))
    east = await _create(client, h, "East")
    kharkiv = await _create(client, h, "Kharkiv", east["id"])
    assert kharkiv["parent_id"] == east["id"]
    post_a = await make_post(db, group_id=kharkiv["id"])
    post_b = await make_post(db, group_id=east["id"])
    post_c = await make_post(db)

    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=h)).json()
    east_node = next(g for g in tree["groups"] if g["name"] == "East")
    assert [p["id"] for p in east_node["posts"]] == [post_b.id]
    assert east_node["groups"][0]["name"] == "Kharkiv"
    assert [p["id"] for p in east_node["groups"][0]["posts"]] == [post_a.id]
    assert post_c.id in [p["id"] for p in tree["posts"]]

    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "scope_group"))).all()
    assert [a.action for a in audit] == ["create", "create"]


async def test_name_unique_within_parent(client, db, headers):
    h = headers(await make_admin(db))
    east, west = await _create(client, h, "East"), await _create(client, h, "West")
    await _create(client, h, "Sector 1", east["id"])
    await _create(client, h, "Sector 1", west["id"])
    r = await client.post(ROOT, json={"name": "Sector 1", "parent_id": east["id"]}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"

    await _create(client, h, "North")
    r = await client.post(ROOT, json={"name": "North"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"


async def test_move_into_own_subtree_is_a_cycle(client, db, headers):
    h = headers(await make_admin(db))
    east = await _create(client, h, "East")
    kharkiv = await _create(client, h, "Kharkiv", east["id"])
    r = await client.patch(_group(east["id"]), json={"parent_id": kharkiv["id"]}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_cycle"
    r = await client.patch(_group(east["id"]), json={"parent_id": east["id"]}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_cycle"


async def test_depth_limit(client, db, headers):
    h = headers(await make_admin(db))
    parent = None
    for level in range(MAX_GROUP_DEPTH):
        parent = (await _create(client, h, f"L{level}", parent))["id"]
    r = await client.post(ROOT, json={"name": "too deep", "parent_id": parent}, headers=h)
    assert r.status_code == 409
    assert r.json()["code"] == "scope_group_depth_exceeded" and r.json()["max_depth"] == 6


async def test_move_subtree_respects_depth_limit(client, db, headers):
    h = headers(await make_admin(db))
    parent = None
    for level in range(1, 6):
        parent = (await _create(client, h, f"A{level}", parent))["id"]
    b = await _create(client, h, "B")
    await _create(client, h, "B1", b["id"])

    r = await client.patch(_group(b["id"]), json={"parent_id": parent}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_depth_exceeded"
    assert (await client.get(_group(b["id"]), headers=h)).json()["parent_id"] is None

    # one level less fits: B alone (height 1) under A5 makes level 6
    lone = await _create(client, h, "Lone")
    r = await client.patch(_group(lone["id"]), json={"parent_id": parent}, headers=h)
    assert r.status_code == 200 and r.json()["parent_id"] == parent
    # and back to the root with null
    r = await client.patch(_group(lone["id"]), json={"parent_id": None}, headers=h)
    assert r.status_code == 200 and r.json()["parent_id"] is None


async def test_archive_only_empty_groups(client, db, headers):
    h = headers(await make_admin(db))
    with_post = await _create(client, h, "WithPost")
    await make_post(db, group_id=with_post["id"])
    with_group = await _create(client, h, "WithGroup")
    await _create(client, h, "Child", with_group["id"])
    empty = await _create(client, h, "Empty")

    for g in (with_post, with_group):
        r = await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=g["id"]), headers=h)
        assert r.status_code == 409 and r.json()["code"] == "scope_group_not_empty"

    archive = V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=empty["id"])
    restore = V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=empty["id"])
    r = await client.post(archive, headers=h)
    assert r.status_code == 200 and r.json()["is_deleted"] is True
    r = await client.patch(_group(empty["id"]), json={"name": "x"}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=h)).json()
    assert "Empty" not in [g["name"] for g in tree["groups"]]

    r = await client.post(restore, headers=h)
    assert r.status_code == 200 and r.json()["is_deleted"] is False
    r = await client.post(restore, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_not_archived"


async def test_post_group_id(client, db, headers):
    h = headers(await make_admin(db))
    archived = await _create(client, h, "Old")
    live = await _create(client, h, "Live")
    await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=archived["id"]), headers=h)
    post = await make_post(db)
    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)

    r = await client.put(url, json={"group_id": archived["id"]}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    r = await client.put(url, json={"group_id": live["id"]}, headers=h)
    assert r.status_code == 200 and r.json()["group_id"] == live["id"]
    r = await client.put(url, json={"group_id": None}, headers=h)
    assert r.status_code == 200 and r.json()["group_id"] is None

    r = await client.post(V1.path(PostRoutes.ROOT), json={"name": "N", "code": "NEWG", "group_id": 9999}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    r = await client.post(V1.path(PostRoutes.ROOT), json={"name": "N", "code": "NEWG", "group_id": live["id"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["group_id"] == live["id"]


async def test_list_groups_filters_and_archived_guard(client, db, headers):
    h = headers(await make_admin(db))
    east = await _create(client, h, "East")
    await _create(client, h, "Kharkiv", east["id"])
    r = await client.get(ROOT, params={"parent_id": east["id"]}, headers=h)
    assert [g["name"] for g in r.json()] == ["Kharkiv"]
    r = await client.get(ROOT, params={"name_contains": "ast", "include_deleted": True}, headers=h)
    assert r.status_code == 200 and [g["name"] for g in r.json()] == ["East"]


@pytest.mark.slow
def test_migration_021_downgrade_and_upgrade(tmp_path):
    db_file = tmp_path / "m.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    conn = sqlite3.connect(db_file)
    assert "group_id" in {row[1] for row in conn.execute("pragma table_info(posts)")}
    conn.close()
    down = _run_downgrade(db_file, "020_post_archive")
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert "group_id" not in {row[1] for row in conn.execute("pragma table_info(posts)")}
    assert not conn.execute("select 1 from sqlite_master where name='scope_groups'").fetchone()
    conn.close()
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    assert "group_id" in {row[1] for row in conn.execute("pragma table_info(posts)")}
