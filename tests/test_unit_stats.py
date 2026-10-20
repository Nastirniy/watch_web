"""Observation counters of a corps or brigade: GET /corps/{id}/observation-stats, /brigades/{id}/observation-stats,
by created_at like the post's, with placement (recorded / current); only posts with observation.view count."""
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ObservationRoutes, PostRoutes
from tests.helpers import (assign, make_admin, make_dictionary, make_group, make_post, make_role, make_user,
                           observation_payload)


def _brigade_stats(brigade_id):
    return V1.path(BrigadeRoutes.BRIGADE_OBSERVATION_STATS, brigade_id=brigade_id)


def _corps_stats(corps_id):
    return V1.path(CorpsRoutes.CORPS_OBSERVATION_STATS, corps_id=corps_id)


async def _stats(client, h, path, **params):
    r = await client.get(path, params={"tz": "UTC", **params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _scenario(client, db, h):
    """corps C > brigades B10, B20; post P in B10: o1 there; P moves to B20; o2 there; post Q in C directly: o3"""
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    b10 = await make_group(db, parent_id=corps.id, kind="brigade")
    b20 = await make_group(db, parent_id=corps.id, kind="brigade")
    p = await make_post(db, group_id=b10.id)
    q = await make_post(db, group_id=corps.id)

    async def observe(post_id):
        r = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post_id, ids["signal_type_id"]),
                              headers=h)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    o1 = await observe(p.id)
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=p.id), json={"group_id": b20.id}, headers=h)
    assert r.status_code == 200, r.text
    o2 = await observe(p.id)
    o3 = await observe(q.id)
    return corps, b10, b20, p, q, (o1, o2, o3)


async def test_counts_per_placement(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, b20, _, _, _ = await _scenario(client, db, h)
    for path, recorded, current in ((_brigade_stats(b10.id), 1, 0), (_brigade_stats(b20.id), 1, 2),
                                    (_corps_stats(corps.id), 3, 3)):
        got = await _stats(client, h, path)
        assert got["all_time"]["not_archived"] == recorded and got["placement"] == "recorded", path
        assert got["last_24h"]["not_archived"] == recorded and got["period"]["not_archived"] == recorded
        got = await _stats(client, h, path, placement="current")
        assert got["all_time"]["not_archived"] == current and got["placement"] == "current", path
    got = await _stats(client, h, _brigade_stats(b10.id))
    assert got["unit_id"] == b10.id and got["last_observation_at"] is not None


async def test_archived_pairs(client, db, headers):
    h = headers(await make_admin(db))
    corps, _, b20, _, _, (_, o2, _) = await _scenario(client, db, h)
    r = await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=o2), headers=h)
    assert r.status_code == 200, r.text
    got = await _stats(client, h, _brigade_stats(b20.id), placement="current")
    assert got["all_time"] == {"not_archived": 1, "archived": None}
    got = await _stats(client, h, _brigade_stats(b20.id), placement="current", include_archived="true")
    assert got["all_time"] == {"not_archived": 1, "archived": 1}
    assert got["period"]["not_archived"] == 1 and got["period"]["archived"] == 1


async def test_buckets_equal_the_sum_over_the_posts(client, db, headers):
    h = headers(await make_admin(db))
    corps, _, _, p, q, _ = await _scenario(client, db, h)
    now = datetime.now(timezone.utc)
    window = {"date_from": (now - timedelta(hours=5)).isoformat(), "date_to": (now + timedelta(hours=1)).isoformat(),
              "group_by": "hour"}
    unit = await _stats(client, h, _corps_stats(corps.id), placement="current", **window)
    per_post = [await _stats(client, h, V1.path(PostRoutes.POST_OBSERVATION_STATS, post_id=post.id), **window)
                for post in (p, q)]
    assert [b["not_archived"] for b in unit["period"]["buckets"]] == [
        sum(values) for values in zip(*([b["not_archived"] for b in s["period"]["buckets"]] for s in per_post))]
    assert unit["all_time"]["not_archived"] == sum(s["all_time"]["not_archived"] for s in per_post)


async def test_only_posts_with_observation_view_count(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, b20, _, _, _ = await _scenario(client, db, h)
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.group(b20.id))
    got = await _stats(client, headers(user), _corps_stats(corps.id))
    assert got["all_time"]["not_archived"] == 2      # o1 and o2 at post P (now in B20); o3 at Q is not visible
    got = await _stats(client, headers(user), _brigade_stats(b10.id))
    assert got["all_time"]["not_archived"] == 1      # recorded at B10, its post is visible now


async def test_unit_outside_the_caller_or_of_another_kind_is_404(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _, _, _, _ = await _scenario(client, db, h)
    for path in (_corps_stats(b10.id), _brigade_stats(corps.id), _brigade_stats(99999)):
        r = await client.get(path, headers=h)
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", path
    other = await make_group(db, kind="corps")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.group(corps.id))
    r = await client.get(_corps_stats(other.id), headers=headers(user))
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    r = await client.get(_corps_stats(corps.id), headers=headers(user))
    assert r.status_code == 200, r.text


async def test_archived_unit_keeps_its_history(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _, _, _, _ = await _scenario(client, db, h)
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=b10.id), headers=h)
    assert r.status_code == 200, r.text
    got = await _stats(client, h, _brigade_stats(b10.id))
    assert got["all_time"]["not_archived"] == 1


async def _observe(client, h, post_id, signal_type_id):
    r = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post_id, signal_type_id), headers=h)
    assert r.status_code == 200, r.text


async def test_brigade_commander_gets_stats_of_its_corps(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    other = await make_group(db, parent_id=corps.id, kind="brigade")
    in_brigade = await make_post(db, group_id=brigade.id)
    in_other = await make_post(db, group_id=other.id)
    for post in (in_brigade, in_brigade, in_other):
        await _observe(client, h, post.id, ids["signal_type_id"])
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.group(brigade.id))
    for placement in ("recorded", "current"):
        got = await _stats(client, headers(user), _corps_stats(corps.id), placement=placement)
        assert got["all_time"]["not_archived"] == 2 and got["unit_id"] == corps.id, placement
    r = await client.get(_corps_stats(other.id), headers=headers(user))
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"


async def test_post_viewer_gets_stats_of_its_brigade_counting_only_its_post(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    mine = await make_post(db, group_id=brigade.id)
    other = await make_post(db, group_id=brigade.id)
    for post in (mine, other, other):
        await _observe(client, h, post.id, ids["signal_type_id"])
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.post(mine.id))
    for placement in ("recorded", "current"):
        got = await _stats(client, headers(user), _brigade_stats(brigade.id), placement=placement)
        assert got["all_time"]["not_archived"] == 1 and got["period"]["not_archived"] == 1, placement
    got = await _stats(client, headers(user), _corps_stats(corps.id))
    assert got["all_time"]["not_archived"] == 1


async def test_stats_of_a_unit_with_no_countable_post_still_404(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    outside = await make_post(db)
    inside = await make_post(db, group_id=brigade.id)
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.post(outside.id))
    for path in (_brigade_stats(brigade.id), _corps_stats(corps.id)):
        r = await client.get(path, headers=headers(user))
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", path
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW]), Node.post(inside.id))
    r = await client.get(_brigade_stats(brigade.id), headers=headers(user))
    assert r.status_code == 200, r.text
