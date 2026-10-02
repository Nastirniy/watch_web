"""Observation counters of a post: GET /posts/{post_id}/observation-stats, the only place they are served."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.backend.config import settings
from app.backend.enums.routes import PostRoutes, SearchRoutes
from app.backend.api.v1 import V1
from tests.helpers import make_post, make_user, make_dictionary, make_observation

POSTS = V1.path(PostRoutes.ROOT)
POST = V1.path(PostRoutes.POST_BY_ID)
STATS = V1.path(PostRoutes.POST_OBSERVATION_STATS)
SEARCH_POSTS = V1.path(SearchRoutes.POSTS)

VIEW = ["post.view", "observation.view"]


async def observation_at(db, user, post, signal_type_id, created_at: datetime, archived: bool = False):
    observation = await make_observation(db, user, post, signal_type_id)
    observation.created_at = created_at
    observation.is_deleted = archived
    db.add(observation)
    await db.commit()
    return observation


@pytest.mark.asyncio
async def test_all_time_and_last_24h(client, db, headers):
    post = await make_post(db)
    other = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    now = datetime.now(timezone.utc)
    # The latest one must be today in the server zone, or the test fails in the first hour after midnight
    start_of_day = now.astimezone(ZoneInfo(settings.timezone.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = max(now - timedelta(hours=1), start_of_day.astimezone(timezone.utc))
    await observation_at(db, user, post, ids["signal_type_id"], recent)
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(hours=30))
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(days=100))
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(hours=2), archived=True)
    await observation_at(db, user, other, ids["signal_type_id"], now - timedelta(hours=1))

    url = STATS.format(post_id=post.id)
    body = (await client.get(url, headers=headers(user))).json()
    assert body["last_observation_at"].startswith(recent.isoformat()[:16])
    assert body["days_without_observations"] == 0
    assert body["all_time"] == {"not_archived": 3, "archived": None}
    assert body["last_24h"] == {"not_archived": 1, "archived": None}
    # default period: the last 30 days
    assert body["period"]["not_archived"] == 2 and body["period"]["archived"] is None

    # Archived observations only on explicit request, separately; counts need observation.view only
    body = (await client.get(url, params={"include_archived": "true"}, headers=headers(user))).json()
    assert body["all_time"] == {"not_archived": 3, "archived": 1}
    assert body["last_24h"] == {"not_archived": 1, "archived": 1}
    assert body["period"]["not_archived"] == 2 and body["period"]["archived"] == 1

    # Post responses carry no counters: the stats route is the only source
    post_body = (await client.get(POST.format(post_id=post.id), headers=headers(user))).json()
    assert "observation_counts" not in post_body
    searched = (await client.get(SEARCH_POSTS, headers=headers(user))).json()
    assert all("observation_counts" not in item for item in searched["items"])

    # Administrators count any post, like GET /statistics; everyone else only their data scope
    admin = await make_user(db, perms=VIEW + ["admin.true"], post_id=other.id)
    as_admin = await client.get(url, headers=headers(admin))
    assert as_admin.status_code == 200, as_admin.text
    assert as_admin.json()["all_time"]["not_archived"] == 3
    outsider = await make_user(db, perms=VIEW, post_id=other.id)
    assert (await client.get(url, headers=headers(outsider))).status_code == 403
    no_observations = await make_user(db, perms=["post.view"], post_id=post.id)
    assert (await client.get(url, headers=headers(no_observations))).status_code == 403
    assert (await client.get(STATS.format(post_id=999999), headers=headers(admin))).status_code == 404


@pytest.mark.asyncio
async def test_period_buckets(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    await observation_at(db, user, post, ids["signal_type_id"], day + timedelta(hours=1))
    await observation_at(db, user, post, ids["signal_type_id"], day + timedelta(hours=1, minutes=30))
    await observation_at(db, user, post, ids["signal_type_id"], day + timedelta(days=2, hours=10))
    await observation_at(db, user, post, ids["signal_type_id"], day + timedelta(hours=5), archived=True)
    # 22:30 UTC on the 19th is the 20th in Kyiv (UTC+3)
    await observation_at(db, user, post, ids["signal_type_id"], day - timedelta(hours=1, minutes=30))

    url = STATS.format(post_id=post.id)
    period = {"date_from": "2026-09-19T00:00:00Z", "date_to": "2026-09-22T23:59:59Z"}

    body = (await client.get(url, params={**period, "tz": "UTC"}, headers=headers(user))).json()["period"]
    assert body["group_by"] == "day" and body["tz"] == "UTC"
    assert body["not_archived"] == 4 and body["archived"] is None
    assert [b["not_archived"] for b in body["buckets"]] == [1, 2, 0, 1]
    assert body["buckets"][0]["start"].startswith("2026-09-19T00:00:00")

    kyiv = (await client.get(url, params={**period, "tz": "Europe/Kyiv"}, headers=headers(user))).json()["period"]
    assert [b["not_archived"] for b in kyiv["buckets"]] == [0, 3, 0, 1, 0]
    assert kyiv["buckets"][1]["start"] == "2026-09-20T00:00:00+03:00"

    hourly = await client.get(url, params={"date_from": "2026-09-20T00:00:00Z", "date_to": "2026-09-20T05:59:59Z", "group_by": "hour", "tz": "UTC"}, headers=headers(user))
    assert hourly.status_code == 200, hourly.text
    assert [b["not_archived"] for b in hourly.json()["period"]["buckets"]] == [0, 2, 0, 0, 0, 0]

    body = (await client.get(url, params={**period, "tz": "UTC", "include_archived": "true"}, headers=headers(user))).json()["period"]
    assert body["not_archived"] == 4 and body["archived"] == 1
    assert [(b["not_archived"], b["archived"]) for b in body["buckets"]] == [(1, 0), (2, 1), (0, 0), (1, 0)]

    reversed_period = await client.get(url, params={"date_from": "2026-09-22T00:00:00Z", "date_to": "2026-09-19T00:00:00Z"}, headers=headers(user))
    assert reversed_period.status_code == 400
    too_long = await client.get(url, params={"date_from": "2026-01-01T00:00:00Z", "date_to": "2026-09-01T00:00:00Z", "group_by": "hour"}, headers=headers(user))
    assert too_long.status_code == 400
    bad_zone = await client.get(url, params={**period, "tz": "Mars/Base"}, headers=headers(user))
    assert bad_zone.status_code == 400


@pytest.mark.asyncio
async def test_days_without_observations(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    url = STATS.format(post_id=post.id)

    empty = (await client.get(url, headers=headers(user))).json()
    assert empty["last_observation_at"] is None and empty["days_without_observations"] is None

    now = datetime.now(timezone.utc)
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(days=40, hours=3))
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(days=5, hours=2))
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(days=1), archived=True)
    body = (await client.get(url, params={"include_archived": "true", "tz": "UTC"}, headers=headers(user))).json()
    generated = datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00"))
    assert body["days_without_observations"] == (generated.date() - (now - timedelta(days=5, hours=2)).date()).days
    assert body["last_observation_at"].startswith((now - timedelta(days=5, hours=2)).isoformat()[:16])


@pytest.mark.asyncio
async def test_days_without_observations_are_calendar_days_of_the_zone(client, db, headers):
    """23:30 Kyiv yesterday seen at 00:30 Kyiv today is one day, though only an hour has passed"""
    from app.backend.services.observation_counts import post_observation_stats
    from app.backend.enums.general import StatsGroupBy

    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    now = datetime(2026, 9, 23, 21, 30, tzinfo=timezone.utc)  # 00:30 on the 24th in Kyiv (UTC+3)
    await observation_at(db, user, post, ids["signal_type_id"], now - timedelta(hours=1))  # 23:30 on the 23rd in Kyiv

    kyiv = await post_observation_stats(db, post.id, None, None, StatsGroupBy.DAY, "Europe/Kyiv", False, now=now)
    assert kyiv.days_without_observations == 1
    utc = await post_observation_stats(db, post.id, None, None, StatsGroupBy.DAY, "UTC", False, now=now)
    assert utc.days_without_observations == 0
