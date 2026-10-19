"""Time zones: every datetime a client sends is stored and compared as its UTC instant (the drivers drop tzinfo, the
database holds naive UTC), and every datetime the API answers carries an offset."""
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, List, get_args, get_origin

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1.models.request.observations import ObservationReportParams
from app.backend.enums.general import ObservationReportType
from app.backend.enums.routes import (
    AuthRoutes, MediaRoutes, ObservationRoutes, PostRoutes, SearchRoutes, StatisticsRoutes,
)
from app.backend.filters.observation_filter import ObservationCriteria, ObservationFilterService
from app.backend.filters.observation_report_filter import ObservationReportCriteria, ObservationReportFilter
from app.backend.models.observation import Observation
from app.main import fastapi_app
from tests.helpers import make_dictionary, make_observation, make_post, make_user, observation_payload
from tests.test_media_uploads import storage  # noqa: F401  fixture

CREATE = V1.path(ObservationRoutes.CREATE)
OBSERVATION = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
DISAPPEARED = V1.path(ObservationRoutes.MARK_DISAPPEARED)
LIST_ALL = V1.path(ObservationRoutes.LIST_ALL)
SEARCH = V1.path(SearchRoutes.OBSERVATIONS)
STATISTICS = V1.path(StatisticsRoutes.ROOT)
STATS = V1.path(PostRoutes.POST_OBSERVATION_STATS)
ME = V1.path(AuthRoutes.ME)
UPLOADS = V1.path(MediaRoutes.UPLOADS)
UPLOAD_SESSION = V1.path(MediaRoutes.UPLOAD_SESSION)

WRITE = ["observation.view", "observation.create", "observation.update"]
KYIV_SUMMER = timezone(timedelta(hours=3))
DATETIME_TEXT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(?P<offset>Z|[+-]\d\d:\d\d)?$")


def datetimes_without_offset(body, where: str = "") -> List[str]:
    """Paths of datetime strings in a JSON body that carry no offset"""
    if isinstance(body, dict):
        return [path for key, value in body.items() for path in datetimes_without_offset(value, f"{where}.{key}")]
    if isinstance(body, list):
        return [path for i, value in enumerate(body) for path in datetimes_without_offset(value, f"{where}[{i}]")]
    if isinstance(body, str):
        match = DATETIME_TEXT.match(body)
        if match and not match.group("offset"):
            return [f"{where}={body}"]
    return []


def datetimes_in(body) -> int:
    if isinstance(body, dict):
        return sum(datetimes_in(value) for value in body.values())
    if isinstance(body, list):
        return sum(datetimes_in(value) for value in body)
    return int(isinstance(body, str) and DATETIME_TEXT.match(body) is not None)


async def stored(db, column, observation_id: int):
    """The value in the database (a column select, never an object of the identity map)"""
    return (await db.exec(select(column).where(Observation.id == observation_id))).one()


async def observation_at(db, user, post, signal_type_id: int, observed_at: datetime = None, created_at: datetime = None):
    observation = await make_observation(db, user, post, signal_type_id)
    if observed_at is not None:
        observation.observed_at = observed_at
    if created_at is not None:
        observation.created_at = created_at
    db.add(observation)
    await db.commit()
    return observation


@pytest.mark.asyncio
async def test_create_with_an_offset_stores_and_returns_the_utc_instant(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=WRITE, post_id=post.id)
    payload = {**observation_payload(post.id, ids["signal_type_id"]), "observed_at": "2026-09-20T10:00:00+03:00"}

    created = await client.post(CREATE, json=payload, headers=headers(user))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["observed_at"] == "2026-09-20T07:00:00Z"
    assert await stored(db, Observation.observed_at, body["id"]) == datetime(2026, 9, 20, 7, 0)
    assert datetimes_without_offset(body) == []


@pytest.mark.asyncio
async def test_a_range_filter_with_offset_bounds_finds_the_row(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view"], post_id=post.id)
    observation = await observation_at(
        db, user, post, ids["signal_type_id"], observed_at=datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 9, 20, 7, 30, tzinfo=timezone.utc),
    )
    around = {"observed_at_from": "2026-09-20T09:30:00+03:00", "observed_at_to": "2026-09-20T10:30:00+03:00"}
    after = {"observed_at_from": "2026-09-20T10:30:00+03:00", "observed_at_to": "2026-09-20T11:30:00+03:00"}
    created = {"created_at_from": "2026-09-20T10:15:00+03:00", "created_at_to": "2026-09-20T10:45:00+03:00"}

    for url in (SEARCH, LIST_ALL):
        found = await client.get(url, params=around, headers=headers(user))
        assert found.status_code == 200, found.text
        items = found.json()["items"] if url == SEARCH else found.json()
        assert [item["id"] for item in items] == [observation.id], url
        missed = await client.get(url, params=after, headers=headers(user))
        assert (missed.json()["items"] if url == SEARCH else missed.json()) == [], url
        by_created = await client.get(url, params=created, headers=headers(user))
        assert [item["id"] for item in (by_created.json()["items"] if url == SEARCH else by_created.json())] == [observation.id], url

    # The report body converts the same way
    report = ObservationReportParams(report_type=ObservationReportType.ALL_FIELDS, **around)
    assert report.observed_at_from == datetime(2026, 9, 20, 6, 30, tzinfo=timezone.utc)
    bands = (await db.exec(ObservationReportFilter.build_query(report))).all()
    assert {band.observation_id for band in bands} == {observation.id}


@pytest.mark.asyncio
async def test_filters_compare_the_utc_instant_of_criteria_built_without_the_api_models(db):
    """A version that builds the criteria itself may hand over any offset: the filter compares the UTC instant"""
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await observation_at(
        db, user, post, ids["signal_type_id"], observed_at=datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc),
    )
    low, high = datetime(2026, 9, 20, 9, 30, tzinfo=KYIV_SUMMER), datetime(2026, 9, 20, 10, 30, tzinfo=KYIV_SUMMER)

    query = ObservationFilterService.apply_filters(
        select(Observation), ObservationCriteria(observed_at_from=low, observed_at_to=high),
    )
    assert [row.id for row in (await db.exec(query)).all()] == [observation.id]

    report = ObservationReportCriteria(
        report_type=ObservationReportType.ALL_FIELDS, observed_at_from=low, observed_at_to=high,
    )
    assert {band.observation_id for band in (await db.exec(ObservationReportFilter.build_query(report))).all()} == {observation.id}


@pytest.mark.asyncio
async def test_mark_disappeared_duration_across_an_offset(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=WRITE, post_id=post.id)
    payload = {**observation_payload(post.id, ids["signal_type_id"]), "observed_at": "2026-09-20T10:00:00+03:00"}
    observation_id = (await client.post(CREATE, json=payload, headers=headers(user))).json()["id"]

    marked = await client.post(
        DISAPPEARED.format(observation_id=observation_id), json={"disappeared_at": "2026-09-20T11:00:00+03:00"},
        headers=headers(user),
    )
    assert marked.status_code == 200, marked.text
    assert await stored(db, Observation.duration_seconds, observation_id) == 3600
    assert await stored(db, Observation.disappeared_at, observation_id) == datetime(2026, 9, 20, 8, 0)

    listed = (await client.get(LIST_ALL, params={"id": observation_id}, headers=headers(user))).json()
    assert listed[0]["disappeared_at"] == "2026-09-20T08:00:00Z"
    assert listed[0]["duration_seconds"] == 3600


@pytest.mark.asyncio
async def test_statistics_and_observation_stats_agree_for_an_offset_period(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["statistics.view", "post.view", "observation.view"], post_id=post.id)
    for hour, minute in ((6, 30), (7, 30), (9, 0), (9, 45)):
        await observation_at(db, user, post, ids["signal_type_id"],
                             created_at=datetime(2026, 9, 20, hour, minute, tzinfo=timezone.utc))
    # 07:00Z - 09:30Z: the observations of 07:30 and 09:00
    period = {"date_from": "2026-09-20T10:00:00+03:00", "date_to": "2026-09-20T12:30:00+03:00"}

    statistics = await client.get(STATISTICS, params=period, headers=headers(user))
    assert statistics.status_code == 200, statistics.text
    statistics = statistics.json()
    stats = await client.get(STATS.format(post_id=post.id), params={**period, "group_by": "hour", "tz": "UTC"},
                             headers=headers(user))
    assert stats.status_code == 200, stats.text
    stats = stats.json()

    assert statistics["observations"]["total_in_period"] == 2
    assert stats["period"]["not_archived"] == 2
    assert statistics["period"] == {"date_from": "2026-09-20T07:00:00Z", "date_to": "2026-09-20T09:30:00Z"}
    assert (stats["period"]["date_from"], stats["period"]["date_to"]) == ("2026-09-20T07:00:00Z", "2026-09-20T09:30:00Z")


@pytest.mark.asyncio
async def test_list_put_and_me_answer_with_an_offset(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=WRITE, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])

    listed = await client.get(LIST_ALL, headers=headers(user))
    assert listed.status_code == 200, listed.text
    assert datetimes_in(listed.json()) > 0
    assert datetimes_without_offset(listed.json()) == []

    updated = await client.put(
        OBSERVATION.format(observation_id=observation.id), json={"observed_at": "2026-09-20T12:00:00+03:00"},
        headers=headers(user),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["observed_at"] == "2026-09-20T09:00:00Z"
    assert await stored(db, Observation.observed_at, observation.id) == datetime(2026, 9, 20, 9, 0)
    assert datetimes_without_offset(updated.json()) == []

    me = await client.get(ME, headers=headers(user))
    assert me.status_code == 200, me.text
    assert me.json()["last_login_at"].endswith("Z"), "derived from the login sessions, stored naive UTC"
    assert me.json()["last_activity_at"] is not None
    assert datetimes_without_offset(me.json()) == []


@pytest.mark.asyncio
async def test_upload_expires_header_carries_an_offset(client, db, headers, storage):
    """The header is documented as date-time; the row read back from the database is naive UTC"""
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    body = {"observation_id": observation.id, "media_type": "image", "original_filename": "photo.png",
            "expected_size": 10, "content_type": "image/png"}
    created = await client.post(UPLOADS, json=body, headers=headers(user))
    assert created.status_code == 201, created.text

    head = await client.head(UPLOAD_SESSION.format(upload_id=created.json()["id"]), headers=headers(user))
    assert head.status_code == 200, head.text
    assert datetimes_without_offset(head.headers["upload-expires"]) == []
    assert DATETIME_TEXT.match(head.headers["upload-expires"])


def _bare_datetimes(annotation, metadata, marker, where: str, seen: set) -> List[str]:
    """Places of a datetime in a type that do not carry the marker (the shared input or output rule)"""
    if annotation is datetime:
        return [] if marker in metadata else [where]
    if get_origin(annotation) is Annotated:
        inner, *extra = get_args(annotation)
        return _bare_datetimes(inner, [*metadata, *extra], marker, where, seen)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return []
        seen.add(annotation)
        return [
            place
            for name, field in annotation.model_fields.items()
            for place in _bare_datetimes(field.annotation, field.metadata, marker, f"{annotation.__name__}.{name}", seen)
        ]
    return [place for arg in get_args(annotation) for place in _bare_datetimes(arg, [], marker, where, seen)]


def _api_routes() -> List[APIRoute]:
    return [route for route in fastapi_app.routes if isinstance(route, APIRoute)]


def test_every_datetime_a_client_sends_is_converted_to_utc():
    """Bodies and query parameters (query models included) of every route type their datetimes as UtcDatetime"""
    from app.backend.utils.validators import UtcDatetime
    marker = get_args(UtcDatetime)[1]
    bare = []
    for route in _api_routes():
        flat = get_flat_dependant(route.dependant)
        for param in [*flat.query_params, *flat.body_params]:
            info = param.field_info
            bare += [f"{route.path} {place}" for place in
                     _bare_datetimes(info.annotation, info.metadata, marker, param.name, set())]
    assert bare == [], "type these as UtcDatetime (app/backend/utils/validators.py):\n" + "\n".join(bare)


def test_every_datetime_of_a_response_is_written_with_an_offset():
    """Every datetime of a response model is typed as ResponseDatetime"""
    from app.backend.utils.validators import ResponseDatetime
    marker = get_args(ResponseDatetime)[1]
    bare = []
    for route in _api_routes():
        if route.response_model is not None:
            bare += [f"{route.path} {place}" for place in
                     _bare_datetimes(route.response_model, [], marker, "response", set())]
    assert bare == [], "type these as ResponseDatetime (app/backend/utils/validators.py):\n" + "\n".join(sorted(set(bare)))


def test_shared_types():
    from pydantic import TypeAdapter
    from app.backend.utils.validators import ResponseDatetime, UtcDatetime
    incoming = TypeAdapter(UtcDatetime)
    assert incoming.validate_python("2026-09-20T10:00:00+03:00") == datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
    assert incoming.validate_python("2026-09-20T10:00:00+03:00").tzinfo == timezone.utc
    assert incoming.validate_python("2026-09-20T10:00:00") == datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)

    outgoing = TypeAdapter(ResponseDatetime)
    assert outgoing.dump_json(datetime(2026, 9, 20, 7, 0)) == b'"2026-09-20T07:00:00Z"'
    # An aware value keeps its offset (observation-stats buckets are in the requested zone)
    assert outgoing.dump_json(datetime(2026, 9, 20, 10, 0, tzinfo=KYIV_SUMMER)) == b'"2026-09-20T10:00:00+03:00"'
