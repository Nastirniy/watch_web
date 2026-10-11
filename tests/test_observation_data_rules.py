"""Observation data rules: band centre inside the band, disappeared time and duration, locking band read,
spreadsheet formula escaping in reports and the tz query parameter."""
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import openpyxl
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1.models.request.observations import ObservationFrequencyCreate
from app.backend.enums.general import ObservationReportType
from app.backend.enums.routes import ObservationRoutes, PostRoutes
from app.backend.filters.observation_report_filter import ObservationReportCriteria
from app.backend.models.observation import Observation, ObservationFrequency
from app.backend.services import observation_counts
from app.backend.services.observation_report_service import ObservationReportService, escape_text
from app.backend.errors.coded import CodedError
from tests.helpers import make_dictionary, make_observation, make_post, make_user

OPERATOR = ["observation.view", "observation.create", "observation.update", "observation.archive"]
BANDS = V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES)
BAND = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID)
BAND_ARCHIVE = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE)
DISAPPEARED = V1.path(ObservationRoutes.MARK_DISAPPEARED)
OBSERVATION = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
STATS = V1.path(PostRoutes.POST_OBSERVATION_STATS)


async def setup_observation(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR + ["post.access_all", "post.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, ids, user, observation


# --- band centre ---

@pytest.mark.parametrize("center", ["99.999", "200.001"])
def test_new_band_center_must_lie_inside_the_band(center):
    with pytest.raises(ValidationError) as caught:
        ObservationFrequencyCreate(kind="rc", frequency_min="100", frequency_max="200", frequency_center=center)
    error = caught.value.errors()[0]
    assert error["type"] == "frequency_center_out_of_range" and error["loc"] == ("frequency_center",)
    for edge in ("100", "200", "150"):
        assert ObservationFrequencyCreate(kind="rc", frequency_min="100", frequency_max="200", frequency_center=edge)


@pytest.mark.asyncio
async def test_band_put_checks_center_on_the_merged_band(client, db, headers):
    _, _, user, observation = await setup_observation(db)
    band_id = (await db.exec(select(ObservationFrequency.id).where(ObservationFrequency.observation_id == observation.id))).one()
    url = BAND.format(observation_id=observation.id, frequency_id=band_id)

    outside = await client.put(url, json={"frequency_center": "250"}, headers=headers(user))
    assert outside.status_code == 422, outside.text
    assert outside.json()["errors"][0]["code"] == "frequency_center_out_of_range"
    assert outside.json()["errors"][0]["pointer"] == "#/frequency_center"

    # the sent min/max moves the range away from the stored centre of 150 and the centre is sent explicitly
    away = await client.put(url, json={"frequency_min": "300", "frequency_max": "400", "frequency_center": "150"}, headers=headers(user))
    assert away.status_code == 422 and away.json()["errors"][0]["code"] == "frequency_center_out_of_range"

    # a centre alone is checked against the stored range, and a new range recalculates it
    fine = await client.put(url, json={"frequency_center": "120"}, headers=headers(user))
    assert fine.status_code == 200 and Decimal(fine.json()["frequency_center"]) == Decimal("120")
    moved = await client.put(url, json={"frequency_min": "300", "frequency_max": "400"}, headers=headers(user))
    assert moved.status_code == 200 and Decimal(moved.json()["frequency_center"]) == Decimal("350")


@pytest.mark.asyncio
async def test_band_post_refuses_center_outside(client, db, headers):
    _, _, user, observation = await setup_observation(db)
    response = await client.post(BANDS.format(observation_id=observation.id), headers=headers(user), json={
        "kind": "video", "frequency_min": "5720", "frequency_max": "5800", "frequency_center": "6000"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "frequency_center_out_of_range"


# --- locking read of the bands ---

@pytest.mark.asyncio
async def test_archiving_a_band_reads_the_other_bands_with_a_lock(client, db, headers):
    post, ids, user, _ = await setup_observation(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"], bands=[("rc", "100", "200"), ("video", "300", "400")])
    first_id, second_id = (await db.exec(
        select(ObservationFrequency.id).where(ObservationFrequency.observation_id == observation.id).order_by(ObservationFrequency.id))).all()

    statements = []

    def record(state):
        if state.is_select:
            statements.append(str(state.statement.compile(dialect=mysql.dialect())))

    event.listen(Session, "do_orm_execute", record)
    try:
        archived = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=first_id), headers=headers(user))
    finally:
        event.remove(Session, "do_orm_execute", record)
    assert archived.status_code == 200, archived.text

    locking = [s for s in statements if "FROM observation_frequencies" in s and "FOR UPDATE" in s]
    assert locking, "the bands are read with FOR UPDATE after the observation row is locked: " + repr(statements)

    # the last active band still cannot be archived
    last = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=second_id), headers=headers(user))
    assert last.status_code == 409 and last.json()["code"] == "last_frequency_not_archivable"


# --- disappeared_at and duration ---

@pytest.mark.asyncio
async def test_disappeared_before_observed_is_refused(client, db, headers):
    _, _, user, observation = await setup_observation(db)
    url = DISAPPEARED.format(observation_id=observation.id)
    observed = observation.observed_at.replace(tzinfo=timezone.utc)

    before = await client.post(url, json={"disappeared_at": (observed - timedelta(seconds=1)).isoformat()}, headers=headers(user))
    assert before.status_code == 422, before.text
    error = before.json()["errors"][0]
    assert error["code"] == "disappeared_before_observed" and error["pointer"] == "#/disappeared_at"
    await db.refresh(observation)
    assert observation.disappeared_at is None and observation.duration_seconds is None

    same = await client.post(url, json={"disappeared_at": observed.isoformat()}, headers=headers(user))
    assert same.status_code == 200, same.text
    await db.refresh(observation)
    assert observation.duration_seconds == 0


@pytest.mark.asyncio
async def test_changing_observed_at_recomputes_the_duration(client, db, headers):
    _, _, user, observation = await setup_observation(db)
    observed = observation.observed_at.replace(tzinfo=timezone.utc)
    disappeared = observed + timedelta(seconds=600)
    assert (await client.post(DISAPPEARED.format(observation_id=observation.id),
                              json={"disappeared_at": disappeared.isoformat()}, headers=headers(user))).status_code == 200
    url = OBSERVATION.format(observation_id=observation.id)

    earlier = await client.put(url, json={"observed_at": (disappeared - timedelta(seconds=100)).isoformat()}, headers=headers(user))
    assert earlier.status_code == 200, earlier.text
    await db.refresh(observation)
    assert observation.duration_seconds == 100

    after = await client.put(url, json={"observed_at": (disappeared + timedelta(seconds=1)).isoformat()}, headers=headers(user))
    assert after.status_code == 422, after.text
    assert after.json()["errors"][0]["code"] == "observed_after_disappeared"
    assert after.json()["errors"][0]["pointer"] == "#/observed_at"
    await db.refresh(observation)
    assert observation.duration_seconds == 100


# --- report cells ---

@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "=HYPERLINK(\"http://x\")"])
def test_escape_text_neutralises_formula_prefixes(text):
    assert escape_text(text) == "'" + text


@pytest.mark.parametrize("text", ["plain", "", "1 = 1", "a=b"])
def test_escape_text_keeps_other_text(text):
    assert escape_text(text) == text


def test_report_never_writes_user_text_as_a_formula():
    from app.backend.models.post import Post
    from app.backend.models.user import User
    from app.backend.models.observation import SignalType, Tag
    observation = Observation(
        id=1, user_id=1, post_id=1, signal_type_id=1, comment="=cmd|' /C calc'!A0",
        observed_at=datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc),
    )
    observation.user = User(id=1, username="+user", username_alias="@alias")
    observation.post = Post(id=1, name="-post")
    observation.signal_type = SignalType(id=1, name="=sig")
    observation.tags = [Tag(id=1, name="=tag")]
    band = ObservationFrequency(
        id=1, observation_id=1, kind="rc", frequency_min=Decimal("100"), frequency_max=Decimal("200"),
        frequency_center=Decimal("150"), bandwidth=Decimal("100"),
    )
    band.observation = observation

    for report_type in (ObservationReportType.ALL_FIELDS, ObservationReportType.RC_FREQ_PART):
        params = ObservationReportCriteria(report_type=report_type, statuses=["=bad"], confidence_level="@x", tag_ids=[1])
        data = ObservationReportService.generate_excel([band], params, tag_names={1: "=tag"})
        sheet = openpyxl.load_workbook(io.BytesIO(data)).active
        formulas = [c.coordinate for row in sheet.iter_rows() for c in row if c.data_type == "f"]
        assert formulas == []
        texts = [c.value for row in sheet.iter_rows() for c in row if isinstance(c.value, str)]
        assert "'=cmd|' /C calc'!A0" in texts
        assert "'=sig" in texts
        assert any(t.startswith("'=bad") for t in texts)
        # typed values stay typed
        assert 100.0 in [c.value for row in sheet.iter_rows() for c in row]


# --- tz ---

@pytest.mark.asyncio
async def test_long_tz_is_a_validation_error(client, db, headers):
    post, _, user, _ = await setup_observation(db)
    period = {"date_from": "2026-09-19T00:00:00Z", "date_to": "2026-09-22T23:59:59Z"}
    response = await client.get(STATS.format(post_id=post.id), params={**period, "tz": "A" * 65}, headers=headers(user))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    ok = await client.get(STATS.format(post_id=post.id), params={**period, "tz": "UTC"}, headers=headers(user))
    assert ok.status_code == 200


def test_zone_lookup_oserror_is_an_unknown_time_zone(monkeypatch):
    def broken(name):
        raise OSError("File name too long")
    monkeypatch.setattr(observation_counts, "ZoneInfo", broken)
    with pytest.raises(CodedError) as caught:
        observation_counts._zone("Europe/Kyiv")
    assert caught.value.code.value == "unknown_time_zone"
