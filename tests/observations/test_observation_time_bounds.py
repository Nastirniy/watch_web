"""Observation times in request bodies lie between 2000-01-01 and now + 1 day; query filters and stored rows are
not bound by it (one row stored with observed_at 9999-12-31 broke the Excel report)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import ObservationRoutes
from app.backend.models.observation import Observation
from tests.helpers import make_dictionary, make_observation, make_post, make_user, observation_payload

OPERATOR = ["observation.view", "observation.create", "observation.update"]
CREATE = V1.path(ObservationRoutes.CREATE)
LIST_ALL = V1.path(ObservationRoutes.LIST_ALL)
OBSERVATION = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
DISAPPEARED = V1.path(ObservationRoutes.MARK_DISAPPEARED)


async def setup(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    return post, ids, user


def refused(response, pointer: str) -> dict:
    assert response.status_code == 422, response.text
    error = response.json()["errors"][0]
    assert error["code"] == "time_out_of_range" and error["pointer"] == pointer, error
    assert error["earliest"] == "2000-01-01T00:00:00Z"
    assert error["latest"].endswith("Z")
    return error


@pytest.mark.asyncio
async def test_create_refuses_observed_at_before_2000(client, db, headers):
    post, ids, user = await setup(db)
    body = observation_payload(post.id, ids["signal_type_id"]) | {"observed_at": "1999-12-31T23:59:59Z"}
    refused(await client.post(CREATE, json=body, headers=headers(user)), "#/observed_at")


@pytest.mark.asyncio
async def test_create_refuses_observed_at_two_days_ahead(client, db, headers):
    post, ids, user = await setup(db)
    ahead = datetime.now(timezone.utc) + timedelta(days=2)
    body = observation_payload(post.id, ids["signal_type_id"]) | {"observed_at": ahead.isoformat()}
    error = refused(await client.post(CREATE, json=body, headers=headers(user)), "#/observed_at")
    latest = datetime.fromisoformat(error["latest"].replace("Z", "+00:00"))
    assert abs(latest - (datetime.now(timezone.utc) + timedelta(days=1))) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_create_accepts_observed_at_one_hour_ahead(client, db, headers):
    """A client clock a little ahead of the server's is not an error"""
    post, ids, user = await setup(db)
    ahead = datetime.now(timezone.utc) + timedelta(hours=1)
    body = observation_payload(post.id, ids["signal_type_id"]) | {"observed_at": ahead.isoformat()}
    response = await client.post(CREATE, json=body, headers=headers(user))
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_create_accepts_the_earliest_time(client, db, headers):
    post, ids, user = await setup(db)
    body = observation_payload(post.id, ids["signal_type_id"]) | {"observed_at": "2000-01-01T00:00:00Z"}
    response = await client.post(CREATE, json=body, headers=headers(user))
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_update_refuses_observed_at_9999(client, db, headers):
    post, ids, user = await setup(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    response = await client.put(OBSERVATION.format(observation_id=observation.id),
                                json={"observed_at": "9999-12-31T00:00:00Z"}, headers=headers(user))
    refused(response, "#/observed_at")


@pytest.mark.asyncio
async def test_mark_disappeared_refuses_9999(client, db, headers):
    post, ids, user = await setup(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    response = await client.post(DISAPPEARED.format(observation_id=observation.id),
                                 json={"disappeared_at": "9999-12-31T00:00:00Z"}, headers=headers(user))
    refused(response, "#/disappeared_at")


@pytest.mark.asyncio
async def test_update_without_observed_at_keeps_a_stored_9999_row(client, db, headers):
    """Rows stored before the bounds are not touched: an update that does not send the time still works"""
    post, ids, user = await setup(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    row = (await db.exec(select(Observation).where(Observation.id == observation.id))).one()
    row.observed_at = datetime(9999, 12, 31)
    db.add(row)
    await db.commit()
    response = await client.put(OBSERVATION.format(observation_id=observation.id),
                                json={"comment": "x"}, headers=headers(user))
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_filters_accept_any_date(client, db, headers):
    _, _, user = await setup(db)
    response = await client.get(LIST_ALL, params={"observed_at_to": "9999-12-31T00:00:00Z",
                                                  "observed_at_from": "1900-01-01T00:00:00Z"}, headers=headers(user))
    assert response.status_code == 200, response.text
