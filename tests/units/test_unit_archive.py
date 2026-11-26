"""Archive of corps and brigades with an optional cascade, restore of the node only, the permissions on the node."""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, PostRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user


def _archive_corps(corps_id: int) -> str:
    return V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps_id)


def _archive_brigade(brigade_id: int) -> str:
    return V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade_id)


async def _rows(db, model, ids):
    rows = (await db.exec(select(model).where(model.id.in_(ids)).order_by(model.id))).all()
    for row in rows:
        await db.refresh(row)
    return {row.id: row for row in rows}


async def _structure(db):
    """corps > brigade > posts p1, p2 (+ archived p3); corps > post p4"""
    corps = await make_group(db, name="Corps 1", kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    p1 = await make_post(db, group_id=brigade.id)
    p2 = await make_post(db, group_id=brigade.id)
    p3 = await make_post(db, group_id=brigade.id)
    p3.is_deleted = True
    db.add(p3)
    await db.commit()
    p4 = await make_post(db, group_id=corps.id)
    return corps, brigade, (p1, p2, p3, p4)


async def _audit(db):
    rows = (await db.exec(select(AuditLog).where(AuditLog.resource_type.in_(["post", "scope_group"]))
                          .order_by(AuditLog.id))).all()
    return [(a.resource_type, a.resource_id, a.action) for a in rows]


async def test_brigade_without_cascade_moves_its_posts_into_the_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p3, p4) = await _structure(db)
    r = await client.post(_archive_brigade(brigade.id), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["is_deleted"] is True and r.json()["id"] == brigade.id
    posts = await _rows(db, Post, [p1.id, p2.id, p3.id])
    assert [(p.group_id, p.is_deleted) for p in posts.values()] == [
        (corps.id, False), (corps.id, False), (brigade.id, True)]
    assert (await _rows(db, ScopeGroup, [brigade.id]))[brigade.id].is_deleted
    assert await _audit(db) == [("post", p1.id, "update"), ("post", p2.id, "update"),
                                ("scope_group", brigade.id, "archive")]


async def test_brigade_with_cascade_archives_its_posts(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p3, p4) = await _structure(db)
    r = await client.post(_archive_brigade(brigade.id), params={"cascade": "true"}, headers=h)
    assert r.status_code == 200, r.text
    posts = await _rows(db, Post, [p1.id, p2.id, p4.id])
    assert [(p.group_id, p.is_deleted) for p in posts.values()] == [
        (brigade.id, True), (brigade.id, True), (corps.id, False)]
    assert await _audit(db) == [("post", p1.id, "archive"), ("post", p2.id, "archive"),
                                ("scope_group", brigade.id, "archive")]


async def test_corps_with_brigades_refuses_without_cascade(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, _, _, p4) = await _structure(db)
    r = await client.post(_archive_corps(corps.id), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "corps_has_brigades"
    groups = await _rows(db, ScopeGroup, [corps.id, brigade.id])
    assert not any(g.is_deleted for g in groups.values())
    assert (await _rows(db, Post, [p4.id]))[p4.id].group_id == corps.id
    assert await _audit(db) == []


async def test_corps_without_cascade_detaches_its_posts(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    post = await make_post(db, group_id=corps.id)
    gone = await make_group(db, parent_id=corps.id, kind="brigade")
    gone.is_deleted = True
    db.add(gone)
    await db.commit()
    r = await client.post(_archive_corps(corps.id), headers=h)
    assert r.status_code == 200, r.text
    row = (await _rows(db, Post, [post.id]))[post.id]
    assert (row.group_id, row.is_deleted) == (None, False)
    assert await _audit(db) == [("post", post.id, "update"), ("scope_group", corps.id, "archive")]


async def test_corps_with_cascade_archives_everything_below(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p3, p4) = await _structure(db)
    before = (await _rows(db, Post, [p3.id]))[p3.id].updated_at
    r = await client.post(_archive_corps(corps.id), params={"cascade": "true"}, headers=h)
    assert r.status_code == 200, r.text
    groups = await _rows(db, ScopeGroup, [corps.id, brigade.id])
    assert all(g.is_deleted for g in groups.values())
    posts = await _rows(db, Post, [p1.id, p2.id, p3.id, p4.id])
    assert all(p.is_deleted for p in posts.values())
    assert posts[p3.id].updated_at == before
    assert sorted(await _audit(db)) == sorted([
        ("scope_group", brigade.id, "archive"), ("post", p1.id, "archive"), ("post", p2.id, "archive"),
        ("post", p4.id, "archive"), ("scope_group", corps.id, "archive")])


async def _limited_user(db, corps, permissions, node=None):
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]), ROOT)
    await assign(db, user, await make_role(db, permissions), node or Node.group(corps.id))
    return user


@pytest.mark.fit_types
async def test_cascade_without_post_archive_at_the_node_changes_nothing(client, db, headers):
    corps, brigade, (p1, _, _, _) = await _structure(db)
    user = await _limited_user(db, corps, [Permissions.POST_UPDATE])
    for path in (_archive_corps(corps.id), _archive_brigade(brigade.id)):
        r = await client.post(path, params={"cascade": "true"}, headers=headers(user))
        assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text
    groups = await _rows(db, ScopeGroup, [corps.id, brigade.id])
    assert not any(g.is_deleted for g in groups.values())
    assert not (await _rows(db, Post, [p1.id]))[p1.id].is_deleted


@pytest.mark.fit_types
async def test_detach_needs_post_update_at_the_node_and_the_destination(client, db, headers):
    corps = await make_group(db, kind="corps")
    post = await make_post(db, group_id=corps.id)
    # post.update at the corps only: the posts would land under the root
    user = await _limited_user(db, corps, [Permissions.POST_UPDATE])
    r = await client.post(_archive_corps(corps.id), headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"
    assert (await _rows(db, Post, [post.id]))[post.id].group_id == corps.id

    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    user = await _limited_user(db, corps, [Permissions.POST_UPDATE], Node.group(brigade.id))
    r = await client.post(_archive_brigade(brigade.id), headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"
    user = await _limited_user(db, corps, [Permissions.POST_UPDATE, Permissions.POST_VIEW])
    r = await client.post(_archive_brigade(brigade.id), headers=headers(user))
    assert r.status_code == 200, r.text


@pytest.mark.fit_types
async def test_archive_needs_scope_group_archive_at_the_node(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.POST_UPDATE]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_ARCHIVE]), Node.group(other.id))
    r = await client.post(_archive_corps(corps.id), headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"


async def test_archived_or_unknown_node_is_404(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, _ = await _structure(db)
    assert (await client.post(_archive_brigade(brigade.id), headers=h)).status_code == 200
    for path in (_archive_brigade(brigade.id), _archive_brigade(corps.id), _archive_corps(brigade.id),
                 _archive_corps(99999)):
        r = await client.post(path, headers=h)
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", path


async def test_restore_brings_back_the_node_only(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, _, _, p4) = await _structure(db)
    assert (await client.post(_archive_corps(corps.id), params={"cascade": "true"}, headers=h)).status_code == 200

    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id), headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"

    r = await client.post(V1.path(CorpsRoutes.CORPS_RESTORE, corps_id=corps.id), headers=h)
    assert r.status_code == 200 and r.json()["is_deleted"] is False
    groups = await _rows(db, ScopeGroup, [brigade.id])
    assert groups[brigade.id].is_deleted
    assert (await _rows(db, Post, [p4.id]))[p4.id].is_deleted

    listed = await client.get(V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=corps.id), params={"include_deleted": "true"},
                              headers=h)
    assert [b["id"] for b in listed.json()] == [brigade.id]
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id), headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_not_archived"
    r = await client.post(V1.path(PostRoutes.POST_RESTORE, post_id=p1.id), headers=h)
    assert r.status_code == 200, r.text
    assert (await _audit(db))[-3:] == [("scope_group", corps.id, "restore"), ("scope_group", brigade.id, "restore"),
                                       ("post", p1.id, "restore")]


async def test_post_create_and_move_into_an_archived_brigade_are_404(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (_, _, _, p4) = await _structure(db)
    assert (await client.post(_archive_brigade(brigade.id), params={"cascade": "true"}, headers=h)).status_code == 200
    r = await client.post(V1.path(PostRoutes.ROOT), json={"name": "N", "code": "N-1", "group_id": brigade.id}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=p4.id), json={"group_id": brigade.id}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"


async def test_post_moves_between_units_still_work(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, _, _, p4) = await _structure(db)
    other = await make_group(db, name="Corps 2", kind="corps")
    for target in (brigade.id, other.id, None, corps.id):
        r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=p4.id), json={"group_id": target}, headers=h)
        assert r.status_code == 200 and r.json()["group_id"] == target, r.text
    r = await client.post(V1.path(PostRoutes.ROOT), json={"name": "N", "code": "N-2", "group_id": brigade.id}, headers=h)
    assert r.status_code == 200 and r.json()["brigade"]["id"] == brigade.id
