"""Findings of the corps and brigades bug hunt (2026-10-20)."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ScopeGroupRoutes
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user


async def _root_user(db, *permissions):
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, *permissions]), ROOT)
    return user


async def test_cascade_and_detach_need_post_view_too(client, db, headers):
    """Write routes require X.view AND the write permission (CLAUDE.md); POST /posts/{id}/archive answers 403 for
    the same caller"""
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    cascade = await _root_user(db, Permissions.SCOPE_GROUP_ARCHIVE, Permissions.POST_ARCHIVE)
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), params={"cascade": "true"},
                          headers=headers(cascade))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text
    detach = await _root_user(db, Permissions.SCOPE_GROUP_ARCHIVE, Permissions.POST_UPDATE)
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), headers=headers(detach))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text
    row = (await db.exec(select(Post).where(Post.id == post.id))).one()
    await db.refresh(row)
    assert (row.group_id, row.is_deleted) == (brigade.id, False)


async def test_brigade_and_group_moves_need_post_view_too(client, db, headers):
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    mover = await _root_user(db, Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_UPDATE)
    for r in (await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": c2.id},
                                 headers=headers(mover)),
              await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id), json={"parent_id": c2.id},
                                 headers=headers(mover))):
        assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text
    row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == brigade.id))).one()
    await db.refresh(row)
    assert row.parent_id == c1.id


async def test_brigade_move_answer_counts_the_moved_posts(client, db, headers):
    """The answer of a move used the tree from before the move (seen as post_count 0 before a move also needed
    post.view at the old corps); the answer now counts from the tree after the move"""
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    await make_post(db, group_id=brigade.id)
    user = await _root_user(db, Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_UPDATE)
    await assign(db, user, await make_role(db, [Permissions.POST_VIEW]), Node.group(c2.id))
    await assign(db, user, await make_role(db, [Permissions.POST_VIEW]), Node.group(c1.id))
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": c2.id},
                           headers=headers(user))
    assert r.status_code == 200, r.text
    assert r.json()["post_count"] == 2


async def test_brigade_create_in_an_unknown_corps_is_404(client, db, headers):
    h = headers(await make_admin(db))
    r = await client.post(V1.path(BrigadeRoutes.ROOT), json={"corps_id": 987654, "name": "B"}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    corps = await make_group(db, kind="corps")
    r = await client.post(V1.path(BrigadeRoutes.ROOT), json={"corps_id": corps.id, "name": "B"}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.get(V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps.id), headers=h)
    assert r.json()["brigade_count"] == 1
