"""Scope groups: nested groups of posts (/scope-groups), the tree, posts.group_id, migration 021."""
import sqlite3

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT as ROOT_NODE
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import AuthRoutes, PostRoutes, ScopeGroupRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Role
from app.backend.services.scope_groups import MAX_GROUP_DEPTH
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user
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


# ----------------------------------------------------------------- visibility by scope (authorization v2)
async def _caller(db, node: Node, *perms, user_type: UserType = UserType.ANALYST):
    """A user holding a custom role with the permissions at the node"""
    user = await make_user(db, user_type=user_type)
    await assign(db, user, await make_role(db, perms), node)
    return user


async def _group_admin(db, node: Node):
    """Type post_admin with the built-in post_admin role (scope_group.view, post.view, ...) at the node"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    role = (await db.exec(select(Role).where(Role.builtin_key == UserType.ADMINISTRATOR.value))).one()
    await assign(db, user, role, node)
    return user


def _names(groups) -> list:
    return [g["name"] for g in groups]


def _post_ids(posts) -> list:
    return [p["id"] for p in posts]


def _every_post_id(tree: dict) -> set:
    """Every post id of a tree response, at any level"""
    ids, stack = set(_post_ids(tree["posts"])), list(tree["groups"])
    while stack:
        group = stack.pop()
        ids |= set(_post_ids(group["posts"]))
        stack.extend(group["groups"])
    return ids


async def test_tree_limited_to_scope(client, db, headers):
    """A group admin of East sees East's subtree only: no West, no post directly under the root"""
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    west = await make_group(db, "West")
    a, a2 = await make_post(db, "A", group_id=kharkiv.id), await make_post(db, "A2", group_id=east.id)
    await make_post(db, "B", group_id=west.id)
    await make_post(db, "Top")
    h = headers(await _group_admin(db, Node.group(east.id)))

    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=h)).json()
    assert _names(tree["groups"]) == ["East"] and tree["posts"] == []
    (east_node,) = tree["groups"]
    assert _post_ids(east_node["posts"]) == [a2.id]
    assert _names(east_node["groups"]) == ["Kharkiv"] and _post_ids(east_node["groups"][0]["posts"]) == [a.id]
    assert _every_post_id(tree) == {a.id, a2.id}

    # A group admin of Kharkiv: Kharkiv is the root of what they see
    h_kharkiv = headers(await _group_admin(db, Node.group(kharkiv.id)))
    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=h_kharkiv)).json()
    assert _names(tree["groups"]) == ["Kharkiv"] and _post_ids(tree["groups"][0]["posts"]) == [a.id]

    # The flat list and one group follow the same scope; outside it a group is not found, like an unknown id
    assert sorted(_names((await client.get(ROOT, headers=h)).json())) == ["East", "Kharkiv"]
    assert (await client.get(_group(kharkiv.id), headers=h)).status_code == 200
    for group_id in (west.id, 999999):
        r = await client.get(_group(group_id), headers=h)
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", r.text
    r = await client.get(ROOT, params={"parent_id": west.id}, headers=h)
    assert r.status_code == 200 and r.json() == []


async def test_group_writes_need_scope_group_view_at_the_group(client, db, headers):
    """The structure permissions are held at the root; a group or a named parent outside scope_group.view answers
    404 like an unknown one"""
    east = await make_group(db, "East")
    west = await make_group(db, "West")
    editor = await _caller(db, ROOT_NODE, Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_UPDATE,
                           Permissions.SCOPE_GROUP_ARCHIVE, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await assign(db, editor, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), Node.group(east.id))
    h = headers(editor)

    for method, url, body in (
            (client.patch, _group(west.id), {"name": "W"}),
            (client.patch, _group(east.id), {"parent_id": west.id}),
            (client.post, V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=west.id), None),
            (client.post, ROOT, {"name": "Under West", "parent_id": west.id}),
    ):
        r = await (method(url, json=body, headers=h) if body is not None else method(url, headers=h))
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", (url, r.text)

    created = await client.post(ROOT, json={"name": "Under East", "parent_id": east.id}, headers=h)
    assert created.status_code == 200, created.text
    # A new name is checked against the names beside the group: scope_group.view at its parent (the root) too
    renamed = await client.patch(_group(east.id), json={"name": "East 2"}, headers=h)
    assert renamed.status_code == 403 and renamed.json()["code"] == "permission_required_for_action", renamed.text
    described = await client.patch(_group(east.id), json={"description": "East front"}, headers=h)
    assert described.status_code == 200, described.text


@pytest.mark.parametrize("group_id", [0, -1])
async def test_group_ids_below_one_are_validation_errors(client, db, headers, group_id):
    """A group id below one is no node of the tree: 422 on every route that takes one (was 500)"""
    h = headers(await make_admin(db))
    for method, route, body in (
            ("GET", ScopeGroupRoutes.GROUP_BY_ID, None),
            ("PATCH", ScopeGroupRoutes.GROUP_BY_ID, {"name": "x"}),
            ("POST", ScopeGroupRoutes.GROUP_ARCHIVE, None),
            ("POST", ScopeGroupRoutes.GROUP_RESTORE, None),
    ):
        r = await client.request(method, V1.path(route, group_id=group_id), json=body, headers=h)
        assert r.status_code == 422, (method, route, r.text)
        problem = r.json()
        assert problem["code"] == "validation_error", problem
        assert [(e["code"], e["parameter"], e["location"]) for e in problem["errors"]] == [
            ("greater_than_equal", "group_id", "path")], problem


async def test_a_group_move_needs_post_update_at_the_old_and_the_new_parent(client, db, headers):
    """R37: a move takes every post below the group along, so it needs post.update covering the old and the new parent
    location, like a post move; checked before the tree rules, nothing moves on a refusal"""
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    west = await make_group(db, "West")
    a = await make_post(db, "A", group_id=kharkiv.id)
    mover = await _caller(db, ROOT_NODE, Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE,
                          user_type=UserType.SYSTEM_ADMINISTRATOR)
    await assign(db, mover, await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]),
                 Node.group(west.id))
    h = headers(mover)
    check = V1.path(AuthRoutes.CHECK_PERMISSION)
    at_a = {"all": ["observation.update"], "post_id": a.id}
    assert (await client.post(check, json=at_a, headers=h)).json()["allowed"] is False

    for parent_id in (west.id, east.id + kharkiv.id + west.id + 1000):   # a live parent and an unknown one alike
        r = await client.patch(_group(kharkiv.id), json={"parent_id": parent_id}, headers=h)
        assert r.status_code == 403, r.text
        body = r.json()
        assert (body["code"], body["action"], body["permission"], body["missing"]) == (
            "permission_required_for_action", "move_scope_group", "post.update", ["post.update"])
    await db.refresh(kharkiv)
    assert kharkiv.parent_id == east.id
    assert (await client.post(check, json=at_a, headers=h)).json()["allowed"] is False
    # A rename is no move
    renamed = await client.patch(_group(kharkiv.id), json={"name": "Kharkiv 2", "parent_id": east.id}, headers=h)
    assert renamed.status_code == 200, renamed.text

    # post.update at both parents: the move to West is allowed, the root is a location of its own
    both = await _caller(db, Node.group(east.id), Permissions.POST_UPDATE, Permissions.POST_VIEW, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await assign(db, both, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]), ROOT_NODE)
    await assign(db, both, await make_role(db, [Permissions.POST_UPDATE, Permissions.POST_VIEW]), Node.group(west.id))
    moved = await client.patch(_group(kharkiv.id), json={"parent_id": west.id}, headers=headers(both))
    assert moved.status_code == 200 and moved.json()["parent_id"] == west.id, moved.text
    r = await client.patch(_group(kharkiv.id), json={"parent_id": None}, headers=headers(both))
    assert r.status_code == 403 and r.json()["action"] == "move_scope_group", r.text

    admin = headers(await make_admin(db))
    r = await client.patch(_group(kharkiv.id), json={"parent_id": None}, headers=admin)
    assert r.status_code == 200 and r.json()["parent_id"] is None, r.text
    r = await client.patch(_group(kharkiv.id), json={"parent_id": 999999}, headers=admin)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", r.text


async def test_tree_posts_follow_post_view(client, db, headers):
    """Groups follow scope_group.view, posts post.view in effect"""
    east = await make_group(db, "East")
    west = await make_group(db, "West")
    a, b = await make_post(db, "A", group_id=east.id), await make_post(db, "B", group_id=west.id)
    await make_post(db, "Top")
    caller = await _caller(db, ROOT_NODE, Permissions.SCOPE_GROUP_VIEW)
    await assign(db, caller, await make_role(db, [Permissions.POST_VIEW]), Node.post(b.id))

    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=headers(caller))).json()
    groups = {g["name"]: g for g in tree["groups"]}
    assert set(groups) >= {"East", "West"}
    assert groups["East"]["posts"] == [] and _post_ids(groups["West"]["posts"]) == [b.id]
    assert _every_post_id(tree) == {b.id}

    # A post-level holder sees no group, only the posts where both permissions hold, at the top of what they see
    holder = await _caller(db, Node.post(a.id), Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW)
    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=headers(holder))).json()
    assert tree["groups"] == [] and _post_ids(tree["posts"]) == [a.id]
    assert (await client.get(ROOT, headers=headers(holder))).json() == []


async def test_tree_leaves_out_a_live_post_of_an_archived_group(client, db, headers):
    """A live post in an archived group (data from before the restore check, test below) keeps its group_id: it is
    not shown as a post of the root"""
    admin = await make_admin(db)
    old = await make_group(db, "Old")
    post = await make_post(db, "Orphan", group_id=old.id)
    old.is_deleted = True
    db.add(old)
    await db.commit()

    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=headers(admin))).json()
    assert "Old" not in _names(tree["groups"])
    assert post.id not in _every_post_id(tree)


async def test_a_post_is_not_restored_into_an_archived_group(client, db, headers):
    """R23: like a group restore under an archived parent, 404 scope_group_not_found; the scope answers first"""
    admin = await make_admin(db)
    h = headers(admin)
    old = await make_group(db, "Old")
    post = await make_post(db, "Orphan", group_id=old.id)
    restore = V1.path(PostRoutes.POST_RESTORE, post_id=post.id)
    for url in (V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=old.id)):
        r = await client.post(url, headers=h)
        assert r.status_code == 200, r.text

    r = await client.post(restore, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", r.text
    await db.refresh(post)
    assert post.is_deleted is True

    other = await make_post(db, "Elsewhere")
    outsider = await _caller(db, Node.post(other.id), Permissions.POST_VIEW, Permissions.POST_ARCHIVE,
                             user_type=UserType.ADMINISTRATOR)
    r = await client.post(restore, headers=headers(outsider))
    assert r.status_code == 403 and r.json()["code"] == "post_access_denied", r.text

    r = await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=old.id), headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(restore, headers=h)
    assert r.status_code == 200 and r.json()["is_deleted"] is False, r.text


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
