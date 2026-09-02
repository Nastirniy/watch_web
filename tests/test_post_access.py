"""Data scope: home post, user_post_access rows, post.access_all, foreign filters."""
import pytest

from app.backend.enums.routes import ObservationRoutes, PostRoutes, UserRoutes
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation, observation_payload

LIST = ObservationRoutes.LIST_ALL.versioned_path()
CREATE = ObservationRoutes.CREATE.versioned_path()
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


def post_access_url(user_id: int, post_id: int) -> str:
    return UserRoutes.USER_POST_ACCESS_BY_POST.versioned_path().format(user_id=user_id, post_id=post_id)


@pytest.mark.asyncio
async def test_home_post_scope(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert {o["id"] for o in listed} == {obs_a.id}
    assert listed[0]["user"]["username"] == admin.username

    foreign = await client.get(LIST, params={"post_id": post_b.id}, headers=headers(user))
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "post_access_denied"

    own_filter = await client.get(LIST, params={"post_id": post_a.id}, headers=headers(user))
    assert own_filter.status_code == 200

    post_b_view = await client.get(PostRoutes.POST_BY_ID.versioned_path().format(post_id=post_b.id), headers=headers(user))
    assert post_b_view.status_code == 403
    assert post_b_view.json()["detail"]["code"] == "post_access_denied"

    create_b = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(user))
    assert create_b.status_code == 403
    assert create_b.json()["detail"]["code"] == "post_access_denied"


@pytest.mark.asyncio
async def test_no_post_assigned(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    homeless = await make_user(db, perms=OPERATOR)
    response = await client.get(PostRoutes.POST_BY_ID.versioned_path().format(post_id=post_a.id), headers=headers(homeless))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "no_post_assigned"
    assert (await client.get(LIST, headers=headers(homeless))).json() == []


@pytest.mark.asyncio
async def test_extra_post_access_read_then_write(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    url = post_access_url(user.id, post_b.id)

    grant = await client.put(url, json={"can_write": False}, headers=headers(admin))
    assert grant.status_code == 200, grant.text
    assert grant.json()["can_write"] is False

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert {o["id"] for o in listed} == {obs_a.id, obs_b.id}

    me = (await client.get("/api/v1/auth/me", headers=headers(user))).json()
    assert me["accessible_post_ids"] == sorted([post_a.id, post_b.id])

    read_only = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(user))
    assert read_only.status_code == 403
    assert read_only.json()["detail"]["code"] == "post_write_denied"

    upgrade = await client.put(url, json={"can_write": True}, headers=headers(admin))
    assert upgrade.status_code == 200 and upgrade.json()["can_write"] is True

    created = await client.post(CREATE, json=observation_payload(post_b.id, ids["signal_type_id"]), headers=headers(user))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["post_id"] == post_b.id
    assert {k: body["user"][k] for k in ("id", "username", "username_alias")} == {"id": user.id, "username": user.username, "username_alias": user.username_alias}
    assert body["disappeared_by_user"] is None

    listing = await client.get(UserRoutes.USER_POST_ACCESS.versioned_path().format(user_id=user.id), headers=headers(admin))
    assert [row["post_id"] for row in listing.json()] == [post_b.id]

    revoke = await client.delete(url, headers=headers(admin))
    assert revoke.status_code == 204
    listed = (await client.get(LIST, headers=headers(user))).json()
    assert {o["id"] for o in listed} == {obs_a.id}

    again = await client.delete(url, headers=headers(admin))
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_post_access_management_validation(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    home = await client.put(post_access_url(user.id, post_a.id), json={"can_write": True}, headers=headers(admin))
    assert home.status_code == 400
    missing_post = await client.put(post_access_url(user.id, 999999), json={}, headers=headers(admin))
    assert missing_post.status_code == 404
    not_admin = await client.put(post_access_url(user.id, post_b.id), json={}, headers=headers(user))
    assert not_admin.status_code == 403
    assert not_admin.json()["detail"]["code"] == "admin_required"


@pytest.mark.asyncio
async def test_post_access_all_sees_everything(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    supervisor = await make_user(db, perms=OPERATOR + ["post.access_all"], post_id=post_a.id)
    listed = (await client.get(LIST, headers=headers(supervisor))).json()
    assert {o["id"] for o in listed} == {obs_a.id, obs_b.id}
    filtered = await client.get(LIST, params={"post_id": post_b.id}, headers=headers(supervisor))
    assert {o["id"] for o in filtered.json()} == {obs_b.id}
    me = (await client.get("/api/v1/auth/me", headers=headers(supervisor))).json()
    assert me["accessible_post_ids"] is None and me["is_admin"] is False


@pytest.mark.asyncio
async def test_user_visibility(client, db, headers):
    post_a, post_b, ids, admin, user, obs_a, obs_b = await world(db)
    viewer = await make_user(db, perms=["user.view"], post_id=post_a.id)
    same_post = await make_user(db, perms=[], post_id=post_a.id)
    other_post = await make_user(db, perms=[], post_id=post_b.id)
    url = UserRoutes.USER_BY_ID.versioned_path()
    assert (await client.get(url.format(user_id=viewer.id), headers=headers(viewer))).status_code == 200
    assert (await client.get(url.format(user_id=same_post.id), headers=headers(viewer))).status_code == 200
    denied = await client.get(url.format(user_id=other_post.id), headers=headers(viewer))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "user_self_only"
    listed = (await client.get(UserRoutes.BASE.versioned_path() + "/", headers=headers(viewer))).json()
    assert other_post.id not in {u["id"] for u in listed}
