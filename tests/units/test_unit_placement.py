"""placement on observation and media filters: recorded (the units stored at the observation's create, the default)
or current (the units its post is in now). Both read today's database; access always follows the current tree."""
from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.enums.general import Permissions
from app.backend.enums.routes import MediaRoutes, ObservationRoutes, PostRoutes, SearchRoutes
from tests.helpers import (assign, make_admin, make_dictionary, make_group, make_media, make_post, make_role, make_user,
                           observation_payload)
from app.backend.models.observation import Observation
from sqlmodel import select

OBSERVATIONS = V1.path(ObservationRoutes.LIST_ALL)
MEDIA = V1.path(MediaRoutes.ROOT)


async def _scenario(client, db, h):
    """corps C > brigades B10, B20; post P in B10: o1 created there, P moves to B20, o2 created there"""
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    b10 = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    b20 = await make_group(db, name="Brigade 20", parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=b10.id)
    other = await make_post(db)

    async def observe(post_id):
        r = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post_id, ids["signal_type_id"]),
                              headers=h)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    o1 = await observe(post.id)
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": b20.id}, headers=h)
    assert r.status_code == 200, r.text
    o2 = await observe(post.id)
    o3 = await observe(other.id)
    return corps, b10, b20, post, (o1, o2, o3)


async def _observations(client, h, params):
    r = await client.get(OBSERVATIONS, params=params, headers=h)
    assert r.status_code == 200, r.text
    listed = {o["id"] for o in r.json()}
    found = await client.get(V1.path(SearchRoutes.OBSERVATIONS), params=params, headers=h)
    assert found.status_code == 200, found.text
    assert {o["id"] for o in found.json()["items"]} == listed and found.json()["total"] == len(listed)
    return listed


async def test_recorded_and_current_after_a_move(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, b20, _, (o1, o2, o3) = await _scenario(client, db, h)
    assert await _observations(client, h, {"brigade_id": b10.id}) == {o1}
    assert await _observations(client, h, {"brigade_id": b10.id, "placement": "recorded"}) == {o1}
    assert await _observations(client, h, {"brigade_id": b20.id}) == {o2}
    assert await _observations(client, h, {"brigade_id": b20.id, "placement": "current"}) == {o1, o2}
    assert await _observations(client, h, {"brigade_id": b10.id, "placement": "current"}) == set()
    assert await _observations(client, h, {"corps_id": corps.id}) == {o1, o2}
    assert await _observations(client, h, {"corps_id": corps.id, "placement": "current"}) == {o1, o2}
    assert await _observations(client, h, {"corps_id": corps.id, "brigade_id": b10.id}) == {o1}
    assert await _observations(client, h, {}) == {o1, o2, o3}


async def test_wrong_kind_or_unknown_unit_gives_nothing(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _, _, _ = await _scenario(client, db, h)
    for placement in ("recorded", "current"):
        assert await _observations(client, h, {"brigade_id": corps.id, "placement": placement}) == set()
        assert await _observations(client, h, {"corps_id": b10.id, "placement": placement}) == set()
        assert await _observations(client, h, {"corps_id": 99999, "placement": placement}) == set()
    r = await client.get(OBSERVATIONS, params={"placement": "then"}, headers=h)
    assert r.status_code == 422


async def test_media_follow_the_placement_of_their_observation(client, db, headers):
    admin = await make_admin(db)
    h = headers(admin)
    _, b10, b20, _, (o1, o2, _) = await _scenario(client, db, h)
    rows = {o.id: o for o in (await db.exec(select(Observation).where(Observation.id.in_([o1, o2])))).all()}
    m1 = await make_media(db, rows[o1], admin)
    m2 = await make_media(db, rows[o2], admin)

    async def media(params):
        r = await client.get(MEDIA, params=params, headers=h)
        assert r.status_code == 200, r.text
        listed = {m["id"] for m in r.json()}
        found = await client.get(V1.path(SearchRoutes.MEDIA), params=params, headers=h)
        assert found.status_code == 200, found.text
        assert {m["id"] for m in found.json()["items"]} == listed and found.json()["total"] == len(listed)
        return listed

    assert await media({"brigade_id": b10.id}) == {m1.id}
    assert await media({"brigade_id": b20.id, "placement": "current"}) == {m1.id, m2.id}
    assert await media({}) >= {m1.id, m2.id}


async def test_access_follows_the_current_tree(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _, _, (o1, _, _) = await _scenario(client, db, h)
    at_b10 = await make_user(db)
    await assign(db, at_b10, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.group(b10.id))
    assert await _observations(client, headers(at_b10), {"brigade_id": b10.id}) == set()
    at_corps = await make_user(db)
    await assign(db, at_corps, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.group(corps.id))
    assert await _observations(client, headers(at_corps), {"brigade_id": b10.id}) == {o1}


async def test_current_leaves_out_posts_that_left_the_unit_or_are_archived(client, db, headers):
    h = headers(await make_admin(db))
    corps, _, b20, post, (o1, o2, _) = await _scenario(client, db, h)
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": None}, headers=h)
    assert r.status_code == 200, r.text
    assert await _observations(client, h, {"corps_id": corps.id, "placement": "current"}) == set()
    assert await _observations(client, h, {"corps_id": corps.id}) == {o1, o2}

    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": b20.id}, headers=h)
    assert r.status_code == 200, r.text
    assert (await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=h)).status_code == 200
    params = {"corps_id": corps.id, "placement": "current", "include_deleted": "true"}
    assert await _observations(client, h, params) == set()
    assert await _observations(client, h, {"corps_id": corps.id, "include_deleted": "true"}) == {o1, o2}
