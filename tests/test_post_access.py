"""Data scope of users shaped like the old model (home post, access rows, post.access_all, admin.true), converted to
role assignments by the factory exactly as migration 023 does; foreign filters. The access-row routes are gone (the
assignment API replaces them)."""
import pytest

from app.backend.enums.routes import AuthRoutes, ObservationRoutes, PostRoutes, SearchRoutes, UserRoutes
from app.backend.api.v1 import V1
from tests.helpers import (
    ACCESS_ALL, ADMIN_TRUE, make_post, make_user, make_admin, make_dictionary, make_observation, observation_payload,
)

LIST = V1.path(ObservationRoutes.LIST_ALL)
CREATE = V1.path(ObservationRoutes.CREATE)
OPERATOR = ["observation.view", "observation.create", "observation.update", "post.view"]


async def world(db):
    post_a = await make_post(db)
    post_b = await make_post(db)
    ids = await make_dictionary(db)
    admin = await make_admin(db)
    user = await make_user(db, perms=OPERATOR, post_id=post_a.id)
    obs_a = await make_observation(db, admin, post_a, ids["signal_type_id"])
    obs_b = await make_observation(db, admin, post_b, ids["signal_type_id"])
    return post_a, post_b, ids, admin, user, obs_a, obs_b


@pytest.mark.asyncio
async def test_home_post_scope(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert {o["id"] for o in listed} == {obs_a.id}
    assert listed[0]["user"]["username"] == admin.username

    foreign = await client.get(LIST, params={"post_id": post_b.id}, headers=headers(user))
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "post_access_denied"

    own_filter = await client.get(LIST, params={"post_id": post_a.id}, headers=headers(user))
    assert own_filter.status_code == 200

    post_b_view = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post_b.id), headers=headers(user))
    assert post_b_view.status_code == 403
    assert post_b_view.json()["code"] == "post_access_denied"

    create_b = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(user))
    assert create_b.status_code == 403
    assert create_b.json()["code"] == "post_access_denied"


@pytest.mark.asyncio
async def test_no_post_assigned(client, db, headers):
    """no_post_assigned: the permission is in effect at no post (its only post is archived). A user with no post at
    all holds scoped permissions nowhere: the routes refuse them (permission_denied)"""
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    gone = await make_post(db)
    unassigned = await make_user(db, perms=OPERATOR, post_id=gone.id)
    gone.is_deleted = True
    db.add(gone)
    await db.commit()
    response = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post_a.id), headers=headers(unassigned))
    assert response.status_code == 403
    assert response.json()["code"] == "no_post_assigned"
    assert (await client.get(LIST, headers=headers(unassigned))).json() == []

    homeless = await make_user(db, perms=OPERATOR)
    response = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post_a.id), headers=headers(homeless))
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied" and response.json()["missing"] == ["post.view"]
    listed = await client.get(LIST, headers=headers(homeless))
    assert listed.status_code == 403 and listed.json()["missing"] == ["observation.view"]


@pytest.mark.asyncio
async def test_extra_post_access_read_then_write(client, db, headers):
    """A read-only access row reads the post's data, a writable one writes it too"""
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    reader = await make_user(db, perms=OPERATOR, post_id=post_a.id, extra_access={post_b.id: False})
    writer = await make_user(db, perms=OPERATOR, post_id=post_a.id, extra_access={post_b.id: True})

    for member in (reader, writer):
        listed = (await client.get(LIST, headers=headers(member))).json()
        assert {o["id"] for o in listed} == {obs_a.id, obs_b.id}
        me = (await client.get(V1.path(AuthRoutes.ME), headers=headers(member))).json()
        assert me["root_permissions"] == []
        assert {post_id for post_id, held in me["post_permissions"].items() if "observation.view" in held} == {
            str(post_a.id), str(post_b.id)}

    read_only = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(reader))
    assert read_only.status_code == 403
    assert read_only.json()["code"] == "post_write_denied"

    created = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(writer))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["post_id"] == post_b.id
    assert {k: body["user"][k] for k in ("id", "username", "username_alias")} == {"id": writer.id, "username": writer.username, "username_alias": writer.username_alias}
    assert body["disappeared_by_user"] is None


@pytest.mark.asyncio
async def test_post_access_all_sees_everything(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    supervisor = await make_user(db, perms=OPERATOR + [ACCESS_ALL], post_id=post_a.id)
    listed = (await client.get(LIST, headers=headers(supervisor))).json()
    assert {o["id"] for o in listed} == {obs_a.id, obs_b.id}
    filtered = await client.get(LIST, params={"post_id": post_b.id}, headers=headers(supervisor))
    assert {o["id"] for o in filtered.json()} == {obs_b.id}
    me = (await client.get(V1.path(AuthRoutes.ME), headers=headers(supervisor))).json()
    assert "observation.view" in me["root_permissions"] and "is_admin" not in me


@pytest.mark.asyncio
async def test_user_visibility(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    viewer = await make_user(db, perms=["user.view"], post_id=post_a.id)
    same_post = await make_user(db, perms=[], post_id=post_a.id)
    other_post = await make_user(db, perms=[], post_id=post_b.id)
    url = V1.path(UserRoutes.USER_BY_ID)
    assert (await client.get(url.format(user_id=viewer.id), headers=headers(viewer))).status_code == 200
    assert (await client.get(url.format(user_id=same_post.id), headers=headers(viewer))).status_code == 200
    denied = await client.get(url.format(user_id=other_post.id), headers=headers(viewer))
    assert denied.status_code == 403
    assert denied.json()["code"] == "user_self_only"
    listed = (await client.get(V1.path(UserRoutes.ROOT), headers=headers(viewer))).json()
    assert other_post.id not in {u["id"] for u in listed}


@pytest.mark.asyncio
async def test_administrators_see_every_post_record_but_not_its_data(client, db, headers):
    """admin.true + post.view: every post record; observations stay limited to the data scope"""
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    manager = await make_user(db, perms=[ADMIN_TRUE, "post.view", "post.update", "observation.view"], post_id=post_a.id)
    posts_url = V1.path(PostRoutes.ROOT)

    listed = await client.get(posts_url, headers=headers(manager))
    assert listed.status_code == 200, listed.text
    assert {post_a.id, post_b.id} <= {p["id"] for p in listed.json()}
    one = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post_b.id), headers=headers(manager))
    assert one.status_code == 200, one.text
    searched = await client.get(V1.path(SearchRoutes.POSTS), headers=headers(manager))
    assert searched.status_code == 200 and post_b.id in {p["id"] for p in searched.json()["items"]}
    updated = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post_b.id), json={"description": "x"}, headers=headers(manager))
    assert updated.status_code == 200, updated.text

    observations = (await client.get(LIST, headers=headers(manager))).json()
    assert {o["id"] for o in observations} == {obs_a.id}, "post records only; observation data keeps the data scope"

    regular = (await client.get(posts_url, headers=headers(user))).json()
    assert {p["id"] for p in regular} == {post_a.id}
    denied = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post_b.id), headers=headers(user))
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_administrators_see_every_user_record(client, db, headers):
    """admin.true + user.view: users of every post; others keep their post scope"""
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    stranger = await make_user(db, perms=["observation.view"], post_id=post_b.id)
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view"], post_id=post_a.id)
    reader = await make_user(db, perms=["user.view"], post_id=post_a.id)
    users_url = V1.path(UserRoutes.ROOT)
    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=stranger.id)

    assert stranger.id in {u["id"] for u in (await client.get(users_url, headers=headers(manager))).json()}
    filtered = await client.get(users_url, params={"post_id": post_b.id}, headers=headers(manager))
    assert filtered.status_code == 200 and {u["id"] for u in filtered.json()} == {stranger.id}
    assert (await client.get(user_url, headers=headers(manager))).status_code == 200
    searched = await client.get(V1.path(SearchRoutes.USERS), headers=headers(manager))
    assert searched.status_code == 200 and stranger.id in {u["id"] for u in searched.json()["items"]}

    assert stranger.id not in {u["id"] for u in (await client.get(users_url, headers=headers(reader))).json()}
    assert (await client.get(users_url, params={"post_id": post_b.id}, headers=headers(reader))).status_code == 403
    assert (await client.get(user_url, headers=headers(reader))).status_code == 403
