"""Datetimes at the edge of the calendar answer 422, never 500.

A value whose UTC instant leaves 0001-01-01 .. 9999-12-31 (0001-01-01T00:00:00+01:00) made `as_utc` raise
OverflowError, which pydantic does not turn into a validation error; and an observation-stats period touching the
first or last days of the calendar overflowed the bucket arithmetic."""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.enums.routes import (BrigadeRoutes, CorpsRoutes, MediaRoutes, ObservationRoutes, PostRoutes,
                                      SearchRoutes, StatisticsRoutes, UserRoutes)
from tests.helpers import (make_admin, make_dictionary, make_group, make_observation, make_post,
                           observation_payload)

CREATE = V1.path(ObservationRoutes.CREATE)
LIST_ALL = V1.path(ObservationRoutes.LIST_ALL)
REPORT = V1.path(ObservationRoutes.REPORT)

# The UTC instant of both values lies outside the calendar
BELOW_MIN = "0001-01-01T00:00:00+01:00"
ABOVE_MAX = "9999-12-31T23:59:59-00:01"
EXTREMES = [BELOW_MIN, ABOVE_MAX]


def refused(response, name: str, code: str = "time_out_of_range") -> dict:
    assert response.status_code == 422, (response.status_code, response.text[:400])
    errors = response.json()["errors"]
    assert errors[0]["code"] == code, errors
    where = errors[0].get("pointer") or errors[0].get("parameter")
    assert where is not None and where.endswith(name), errors[0]
    return errors[0]


# ----------------------------------------------------------------------------------- request bodies (ObservationTime)

@pytest.mark.parametrize("value", EXTREMES)
async def test_create_refuses_a_time_outside_the_calendar(client, db, headers, value):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    post = await make_post(db)
    body = observation_payload(post.id, ids["signal_type_id"]) | {"observed_at": value}
    error = refused(await client.post(CREATE, json=body, headers=h), "observed_at")
    assert error["earliest"] == "2000-01-01T00:00:00Z" and error["latest"].endswith("Z")


@pytest.mark.parametrize("value", EXTREMES)
async def test_update_refuses_a_time_outside_the_calendar(client, db, headers, value):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    post = await make_post(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    r = await client.put(V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=observation.id),
                         json={"observed_at": value}, headers=headers(admin))
    refused(r, "observed_at")


@pytest.mark.parametrize("value", EXTREMES)
async def test_disappeared_refuses_a_time_outside_the_calendar(client, db, headers, value):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    post = await make_post(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    r = await client.post(V1.path(ObservationRoutes.MARK_DISAPPEARED, observation_id=observation.id),
                          json={"disappeared_at": value}, headers=headers(admin))
    refused(r, "disappeared_at")


# ------------------------------------------------------------------------------ filters (UtcDatetime), same code

@pytest.mark.parametrize("value", EXTREMES)
async def test_list_and_search_filters_refuse_a_time_outside_the_calendar(client, db, headers, value):
    h = headers(await make_admin(db))
    for path in (LIST_ALL, V1.path(SearchRoutes.OBSERVATIONS)):
        for name in ("observed_at_from", "observed_at_to", "created_at_from", "created_at_to"):
            error = refused(await client.get(path, params={name: value}, headers=h), name)
            assert error["earliest"] == "0001-01-01T00:00:00Z" and error["latest"] == "9999-12-31T23:59:59Z"


@pytest.mark.parametrize("value", EXTREMES)
async def test_report_refuses_a_time_outside_the_calendar(client, db, headers, value):
    h = headers(await make_admin(db))
    r = await client.post(REPORT, json={"report_type": "all_fields", "observed_at_from": value}, headers=h)
    refused(r, "observed_at_from")


@pytest.mark.parametrize("path,name", [
    (V1.path(MediaRoutes.ROOT), "uploaded_at_from"), (V1.path(SearchRoutes.MEDIA), "uploaded_at_to"),
    (V1.path(PostRoutes.ROOT), "created_at_from"), (V1.path(SearchRoutes.POSTS), "last_user_activity_from"),
    (V1.path(UserRoutes.ROOT), "last_login_from"), (V1.path(SearchRoutes.USERS), "created_at_to"),
    (V1.path(SearchRoutes.ROLES), "created_at_from"), (V1.path(SearchRoutes.SIGNAL_TYPES), "created_at_from"),
    (V1.path(SearchRoutes.TAGS), "created_at_from"), (V1.path(StatisticsRoutes.ROOT), "date_from"),
    (V1.path(StatisticsRoutes.ROOT), "date_to"),
])
@pytest.mark.parametrize("value", EXTREMES)
async def test_every_family_of_datetime_filters_refuses_a_time_outside_the_calendar(client, db, headers, path, name, value):
    h = headers(await make_admin(db))
    refused(await client.get(path, params={name: value}, headers=h), name)


async def test_a_filter_at_the_calendar_edge_in_utc_is_still_accepted(client, db, headers):
    h = headers(await make_admin(db))
    for value in ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z", "0001-01-01T01:00:00+01:00"):
        r = await client.get(LIST_ALL, params={"created_at_from": value}, headers=h)
        assert r.status_code == 200, (value, r.text[:300])


# ----------------------------------------------------------------------------- observation-stats bucket arithmetic

async def stats_paths(db):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    return (V1.path(CorpsRoutes.CORPS_OBSERVATION_STATS, corps_id=corps.id),
            V1.path(BrigadeRoutes.BRIGADE_OBSERVATION_STATS, brigade_id=brigade.id),
            V1.path(PostRoutes.POST_OBSERVATION_STATS, post_id=post.id))


@pytest.mark.parametrize("params,code", [
    ({"date_from": "9999-12-30T00:00:00Z", "date_to": "9999-12-31T23:00:00Z", "tz": "UTC"}, "stats_period_out_of_range"),
    ({"date_from": "9999-12-31T00:00:00Z", "date_to": "9999-12-31T23:00:00Z", "tz": "UTC", "group_by": "hour"},
     "stats_period_out_of_range"),
    ({"date_from": "9999-12-31T00:00:00Z", "date_to": "9999-12-31T22:00:00Z", "tz": "Europe/Kyiv", "group_by": "hour"},
     "stats_period_out_of_range"),
    ({"date_from": "0001-01-01T00:00:00Z", "date_to": "0001-01-02T00:00:00Z", "tz": "Europe/Kyiv"},
     "stats_period_out_of_range"),
    ({"date_from": "0001-01-01T00:00:00Z", "date_to": "0001-01-02T00:00:00Z", "tz": "America/New_York"},
     "stats_period_out_of_range"),
    ({"date_to": "0001-01-10T00:00:00Z", "tz": "UTC"}, "stats_period_out_of_range"),
    ({"date_from": "0001-01-01T00:00:00+05:00", "date_to": "0001-01-02T00:00:00Z", "tz": "UTC"}, "time_out_of_range"),
])
async def test_stats_with_a_period_at_the_calendar_edge_answers_422(client, db, headers, params, code):
    h = headers(await make_admin(db))
    for path in await stats_paths(db):
        for placement in ("recorded", "current"):
            r = await client.get(path, params={**params, "placement": placement}, headers=h)
            assert r.status_code == 422, (path, placement, r.status_code, r.text[:300])
            body = r.json()
            assert (body["errors"][0]["code"] if code == "time_out_of_range" else body["code"]) == code, body


async def test_stats_of_an_ordinary_period_and_of_the_rest_of_the_year_still_answer_200(client, db, headers):
    h = headers(await make_admin(db))
    now = datetime.now(timezone.utc)
    periods = [
        {},
        {"date_from": (now - timedelta(days=3)).isoformat(), "date_to": now.isoformat(), "tz": "Europe/Kyiv"},
        # a client may ask for a period that ends in the future
        {"date_from": now.isoformat(), "date_to": (now + timedelta(days=20)).isoformat(), "tz": "America/New_York"},
        {"date_from": "2000-01-01T00:00:00Z", "date_to": "2000-12-31T00:00:00Z", "tz": "UTC"},
    ]
    for path in await stats_paths(db):
        for params in periods:
            r = await client.get(path, params=params, headers=h)
            assert r.status_code == 200, (path, params, r.text[:300])


@pytest.mark.parametrize("params", [{"date_to": "0001-01-05T00:00:00Z"}, {"date_to": "0001-01-30T23:59:59Z"}])
async def test_statistics_default_start_before_year_1_answers_422(client, db, headers, params):
    """No date_from: the default start (date_to minus 30 days) leaves the calendar"""
    h = headers(await make_admin(db))
    r = await client.get(V1.path(StatisticsRoutes.ROOT), params=params, headers=h)
    assert r.status_code == 422, (r.status_code, r.text[:300])
    assert r.json()["code"] == "stats_period_out_of_range"


async def test_statistics_default_start_inside_the_calendar_still_answers_200(client, db, headers):
    h = headers(await make_admin(db))
    r = await client.get(V1.path(StatisticsRoutes.ROOT), params={"date_to": "0001-02-05T00:00:00Z"}, headers=h)
    assert r.status_code == 200, r.text[:300]
