"""Post writes need post.view where they act, next to the write permission (CLAUDE.md: write routes require X.view
AND X.create / X.update / X.archive, each in effect where it acts): update, archive and restore at the post, a move at
the old and the new location, a create at the location. Custom roles only: the built-in types always pair them.
"""
from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import PostRoutes
from tests.helpers import assign, make_group, make_post, make_role, make_user

P = Permissions


async def _user(db, grants):
    """An administrator holding one custom role per (permissions, node)"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    for perms, node in grants:
        await assign(db, user, await make_role(db, perms), node)
    return user


def _denied(r, code: str, action: str = None) -> None:
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == code, r.text
    assert "post.view" in body["missing"], r.text
    if action is not None:
        assert body["action"] == action, r.text


async def test_update_needs_post_view_at_the_post(client, db, headers):
    hidden = await make_post(db, name="Hidden post")
    seen = await make_post(db)
    url = V1.path(PostRoutes.POST_BY_ID, post_id=hidden.id)
    blind = await _user(db, [([P.POST_UPDATE], ROOT), ([P.POST_VIEW], Node.post(seen.id))])
    r = await client.put(url, json={"name": "Renamed"}, headers=headers(blind))
    _denied(r, "post_access_denied")
    assert "Hidden post" not in r.text
    await db.refresh(hidden)
    assert hidden.name == "Hidden post"

    sighted = await _user(db, [([P.POST_UPDATE], ROOT), ([P.POST_VIEW], Node.post(hidden.id))])
    r = await client.put(url, json={"name": "Renamed"}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["name"] == "Renamed", r.text


async def test_move_needs_post_view_at_the_new_location(client, db, headers):
    corps = await make_group(db, kind="corps")
    b1 = await make_group(db, parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=b1.id)
    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    blind = await _user(db, [([P.POST_UPDATE], Node.group(corps.id)), ([P.POST_VIEW], Node.group(b1.id))])
    _denied(await client.put(url, json={"group_id": b2.id}, headers=headers(blind)),
            "permission_required_for_action", "move_post")
    await db.refresh(post)
    assert post.group_id == b1.id

    sighted = await _user(db, [([P.POST_UPDATE, P.POST_VIEW], Node.group(corps.id))])
    r = await client.put(url, json={"group_id": b2.id}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["group_id"] == b2.id, r.text


async def test_move_needs_post_view_at_the_old_location(client, db, headers):
    """post.view on the post itself shows the post but does not cover the brigade the post leaves"""
    corps = await make_group(db, kind="corps")
    b1 = await make_group(db, parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=b1.id)
    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    blind = await _user(db, [
        ([P.POST_UPDATE], Node.group(corps.id)), ([P.POST_VIEW], Node.post(post.id)), ([P.POST_VIEW], Node.group(b2.id)),
    ])
    _denied(await client.put(url, json={"group_id": b2.id}, headers=headers(blind)),
            "permission_required_for_action", "move_post")
    await db.refresh(post)
    assert post.group_id == b1.id

    sighted = await _user(db, [
        ([P.POST_UPDATE], Node.group(corps.id)), ([P.POST_VIEW], Node.group(b1.id)), ([P.POST_VIEW], Node.group(b2.id)),
    ])
    r = await client.put(url, json={"group_id": b2.id}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["group_id"] == b2.id, r.text


async def test_archive_needs_post_view_at_the_post(client, db, headers):
    hidden = await make_post(db)
    seen = await make_post(db)
    url = V1.path(PostRoutes.POST_ARCHIVE, post_id=hidden.id)
    blind = await _user(db, [([P.POST_ARCHIVE], ROOT), ([P.POST_VIEW], Node.post(seen.id))])
    _denied(await client.post(url, headers=headers(blind)), "post_access_denied")
    await db.refresh(hidden)
    assert hidden.is_deleted is False

    sighted = await _user(db, [([P.POST_ARCHIVE, P.POST_VIEW], ROOT)])
    r = await client.post(url, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["is_deleted"] is True, r.text


async def test_restore_needs_post_view_at_the_post(client, db, headers):
    hidden = await make_post(db)
    hidden.is_deleted = True
    db.add(hidden)
    await db.commit()
    seen = await make_post(db)
    url = V1.path(PostRoutes.POST_RESTORE, post_id=hidden.id)
    blind = await _user(db, [([P.POST_ARCHIVE], ROOT), ([P.POST_VIEW], Node.post(seen.id))])
    _denied(await client.post(url, headers=headers(blind)), "post_access_denied")
    await db.refresh(hidden)
    assert hidden.is_deleted is True

    sighted = await _user(db, [([P.POST_ARCHIVE, P.POST_VIEW], ROOT)])
    r = await client.post(url, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["is_deleted"] is False, r.text


async def test_create_needs_post_view_where_it_creates(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_post(db)
    blind = await _user(db, [([P.POST_CREATE], Node.group(corps.id)), ([P.POST_VIEW], Node.post(other.id))])
    _denied(await client.post(V1.path(PostRoutes.ROOT), json={"name": "x", "code": "PWV1", "group_id": corps.id},
                              headers=headers(blind)), "permission_required_for_action", "create_post")

    sighted = await _user(db, [([P.POST_CREATE, P.POST_VIEW], Node.group(corps.id))])
    r = await client.post(V1.path(PostRoutes.ROOT), json={"name": "x", "code": "PWV1", "group_id": corps.id},
                          headers=headers(sighted))
    assert r.status_code == 200 and r.json()["group_id"] == corps.id, r.text


async def test_create_at_the_root_needs_post_view_at_the_root(client, db, headers):
    other = await make_post(db)
    blind = await _user(db, [([P.POST_CREATE], ROOT), ([P.POST_VIEW], Node.post(other.id))])
    _denied(await client.post(V1.path(PostRoutes.ROOT), json={"name": "x", "code": "PWV2"}, headers=headers(blind)),
            "permission_required_for_action", "create_post")


async def test_view_without_the_write_permission_still_answers_post_write_denied(client, db, headers):
    """post.view first, then the write permission: a caller who sees the post learns which write it lacks"""
    post = await make_post(db)
    other = await make_post(db)
    viewer = await _user(db, [([P.POST_VIEW], Node.post(post.id)),
                              ([P.POST_VIEW, P.POST_UPDATE, P.POST_ARCHIVE], Node.post(other.id))])
    for r, missing in (
            (await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"name": "x"},
                              headers=headers(viewer)), "post.update"),
            (await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(viewer)),
             "post.archive"),
    ):
        assert r.status_code == 403 and r.json()["code"] == "post_write_denied", r.text
        assert r.json()["missing"] == [missing], r.text
