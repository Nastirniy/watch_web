"""Frequency bands of an observation: create, change, archive, filters and reports."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import select

from app.backend.enums.routes import ObservationRoutes
from app.backend.filters.observation_report_filter import ObservationReportFilter
from app.backend.filters.params.observation_report_params import ObservationReportParams
from app.backend.models.observation import Observation, ObservationFrequency
from tests.helpers import make_post, make_user, make_dictionary, make_observation

CREATE = ObservationRoutes.CREATE.versioned_path()
LIST = ObservationRoutes.LIST_ALL.versioned_path()
ARCHIVE_OBSERVATION = ObservationRoutes.OBSERVATION_ARCHIVE.versioned_path()
BANDS = ObservationRoutes.OBSERVATION_FREQUENCIES.versioned_path()
BAND = ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID.versioned_path()
BAND_ARCHIVE = ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE.versioned_path()
BAND_RESTORE = ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE.versioned_path()
OPERATOR = ["observation.view", "observation.create", "observation.update", "observation.archive"]


def payload(post_id, signal_type_id, bands):
    return {
        "post_id": post_id,
        "signal_type_id": signal_type_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "frequencies": bands,
    }


@pytest.mark.asyncio
async def test_create_with_several_bands(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)

    created = await client.post(CREATE, json=payload(post.id, ids["signal_type_id"], [
        {"kind": "rc", "frequency_min": "2400", "frequency_max": "2480", "link_group": 1},
        {"kind": "video", "frequency_min": "5720", "frequency_max": "5800", "link_group": 1},
        {"kind": "rc", "frequency_min": "100.001", "frequency_max": "100.002", "frequency_center": "100.002"},
        {"kind": "video", "frequency_min": "100.001", "frequency_max": "100.002"},
    ]), headers=headers(user))
    assert created.status_code == 200, created.text
    body = created.json()
    assert "frequency_min" not in body and "video_frequency_min" not in body
    bands = body["frequencies"]
    assert [b["kind"] for b in bands] == ["rc", "video", "rc", "video"]
    assert [b["link_group"] for b in bands] == [1, 1, None, None]
    assert Decimal(bands[0]["frequency_center"]) == Decimal("2440")
    assert Decimal(bands[0]["bandwidth"]) == Decimal("80")
    assert Decimal(bands[2]["frequency_center"]) == Decimal("100.002"), "an explicit center is kept"
    assert Decimal(bands[3]["frequency_center"]) == Decimal("100.002"), "default center is rounded to 3 decimals"


@pytest.mark.asyncio
@pytest.mark.parametrize("bands", [
    [],
    [{"kind": "rc", "frequency_min": "200", "frequency_max": "100"}],
    [{"kind": "rc", "frequency_min": "100", "frequency_max": "100"}],
    [{"kind": "rc", "frequency_min": "0", "frequency_max": "100"}],
    [{"kind": "gnss", "frequency_min": "100", "frequency_max": "200"}],
    [{"kind": "rc", "frequency_min": "100.0001", "frequency_max": "200"}],
    [{"kind": "rc", "frequency_min": "100", "frequency_max": "200", "link_group": 0}],
])
async def test_create_rejects_invalid_bands(client, db, headers, bands):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    response = await client.post(CREATE, json=payload(post.id, ids["signal_type_id"], bands), headers=headers(user))
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_band_lifecycle(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    first_id = (await db.exec(select(ObservationFrequency.id).where(ObservationFrequency.observation_id == observation.id))).one()

    added = await client.post(BANDS.format(observation_id=observation.id),
                              json={"kind": "video", "frequency_min": "5720", "frequency_max": "5800"}, headers=headers(user))
    assert added.status_code == 200, added.text
    second_id = added.json()["id"]
    assert Decimal(added.json()["bandwidth"]) == Decimal("80")

    url = BAND.format(observation_id=observation.id, frequency_id=second_id)
    moved = await client.put(url, json={"frequency_max": "5900", "link_group": 2}, headers=headers(user))
    assert moved.status_code == 200, moved.text
    assert Decimal(moved.json()["frequency_center"]) == Decimal("5810"), "center follows min/max when not sent"
    assert Decimal(moved.json()["bandwidth"]) == Decimal("180")
    assert moved.json()["link_group"] == 2
    cleared = await client.put(url, json={"link_group": None}, headers=headers(user))
    assert cleared.status_code == 200 and cleared.json()["link_group"] is None
    reversed_band = await client.put(url, json={"frequency_min": "6000"}, headers=headers(user))
    assert reversed_band.status_code == 422
    assert reversed_band.json()["errors"] == [{"code": "frequency_range_invalid", "detail": "frequency_max must be greater than frequency_min", "pointer": "#/frequency_max"}]
    assert (await client.put(url, json={"kind": None}, headers=headers(user))).status_code == 422
    assert (await client.put(BAND.format(observation_id=observation.id, frequency_id=999999), json={}, headers=headers(user))).status_code == 404

    archived = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=first_id), headers=headers(user))
    assert archived.status_code == 200 and archived.json()["is_deleted"] is True
    again = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=first_id), headers=headers(user))
    assert again.status_code == 404
    assert (await client.put(BAND.format(observation_id=observation.id, frequency_id=first_id), json={"link_group": 1}, headers=headers(user))).status_code == 404

    last = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=second_id), headers=headers(user))
    assert last.status_code == 409, "the last active band stays"

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert [b["id"] for b in listed[0]["frequencies"]] == [second_id], "archived bands are left out of the observation"

    active = await client.get(BANDS.format(observation_id=observation.id), headers=headers(user))
    assert [b["id"] for b in active.json()] == [second_id]
    everything = await client.get(BANDS.format(observation_id=observation.id), params={"include_deleted": "true"}, headers=headers(user))
    assert [b["id"] for b in everything.json()] == [first_id, second_id]

    restored = await client.post(BAND_RESTORE.format(observation_id=observation.id, frequency_id=first_id), headers=headers(user))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    not_archived = await client.post(BAND_RESTORE.format(observation_id=observation.id, frequency_id=first_id), headers=headers(user))
    assert not_archived.status_code == 409


@pytest.mark.asyncio
async def test_band_routes_follow_observation_rules(client, db, headers):
    post = await make_post(db)
    other_post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=OPERATOR, post_id=post.id)
    colleague = await make_user(db, perms=OPERATOR, post_id=post.id)
    outsider = await make_user(db, perms=OPERATOR, post_id=other_post.id)
    viewer = await make_user(db, perms=["observation.view"], post_id=post.id)
    observation = await make_observation(db, owner, post, ids["signal_type_id"])
    band = {"kind": "rc", "frequency_min": "300", "frequency_max": "400"}
    url = BANDS.format(observation_id=observation.id)

    assert (await client.post(url, json=band, headers=headers(colleague))).json()["code"] == "observation_not_owner"
    assert (await client.post(url, json=band, headers=headers(outsider))).status_code == 403
    assert (await client.get(url, headers=headers(outsider))).status_code == 403
    assert (await client.post(url, json=band, headers=headers(viewer))).status_code == 403
    assert (await client.get(url, params={"include_deleted": "true"}, headers=headers(viewer))).status_code == 403

    assert (await client.post(ARCHIVE_OBSERVATION.format(observation_id=observation.id), headers=headers(owner))).status_code == 200
    assert (await client.post(url, json=band, headers=headers(owner))).status_code == 404
    assert (await client.get(url, headers=headers(owner))).status_code == 404
    archived_list = await client.get(url, params={"include_deleted": "true"}, headers=headers(owner))
    assert archived_list.status_code == 200 and len(archived_list.json()) == 1


@pytest.mark.asyncio
async def test_list_filters_match_one_band(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"], bands=[
        ("rc", "100", "200"), ("video", "5700", "5800"),
    ])

    async def found(**params):
        response = await client.get(LIST, params=params, headers=headers(user))
        assert response.status_code == 200, response.text
        return [o["id"] for o in response.json()]

    assert await found(frequency_kind="video", frequency_center_from="5000") == [observation.id]
    assert await found(frequency_kind="rc", frequency_center_from="5000") == [], "kind and range must hold for the same band"
    assert await found(frequency_center_from="140", frequency_center_to="160", bandwidth_from="100") == [observation.id]
    assert await found(frequency_center_from="140", frequency_center_to="160", bandwidth_from="101") == []

    video_id = (await db.exec(select(ObservationFrequency.id).where(ObservationFrequency.kind == "video"))).one()
    assert (await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=video_id), headers=headers(user))).status_code == 200
    assert await found(frequency_kind="video") == [], "archived bands do not match"


@pytest.mark.asyncio
async def test_sort_by_lowest_matching_band_center(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    low_rc = await make_observation(db, user, post, ids["signal_type_id"], bands=[("rc", "100", "200"), ("video", "5900", "6000")])
    high_rc = await make_observation(db, user, post, ids["signal_type_id"], bands=[("rc", "900", "1000"), ("video", "1200", "1300")])

    async def order(**params):
        response = await client.get(LIST, params={"sort_by": "frequency_center", **params}, headers=headers(user))
        assert response.status_code == 200, response.text
        return [o["id"] for o in response.json()]

    assert await order(sort_order="ascending") == [low_rc.id, high_rc.id]
    assert await order(sort_order="descending") == [high_rc.id, low_rc.id]
    assert await order(sort_order="ascending", frequency_kind="video") == [high_rc.id, low_rc.id], \
        "the sort uses the bands selected by the band filters"
    schema = await client.get(ObservationRoutes.FILTER_SCHEMA.versioned_path(), headers=headers(user))
    assert "frequency_center" in schema.json()["sortable_fields"]


@pytest.mark.asyncio
async def test_report_rows_are_bands_of_visible_observations(db):
    post = await make_post(db)
    other_post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    kept = await make_observation(db, user, post, ids["signal_type_id"], bands=[
        ("rc", "100", "200"), ("rc", "300", "400"), ("video", "5700", "5800"),
    ])
    archived = await make_observation(db, user, post, ids["signal_type_id"], bands=[("rc", "100", "200")])
    elsewhere = await make_observation(db, user, other_post, ids["signal_type_id"], bands=[("rc", "100", "200")])
    archived_row = (await db.exec(select(Observation).where(Observation.id == archived.id))).one()
    archived_row.is_deleted = True
    db.add(archived_row)
    await db.commit()

    async def rows(report_type, scope=None, **params):
        query = ObservationReportFilter.build_query(ObservationReportParams(report_type=report_type, **params), scope)
        return [(f.observation_id, f.kind, str(f.frequency_min)) for f in (await db.exec(query)).all()]

    rc_rows = await rows("rc_freq_part", scope={post.id})
    assert [(o, k) for o, k, _ in rc_rows] == [(kept.id, "rc"), (kept.id, "rc")], "archived observation and other post are left out"
    assert [(o, k) for o, k, _ in await rows("video_freq_part")] == [(kept.id, "video")]
    assert len(await rows("all_fields")) == 4, "unscoped: 3 bands of kept + 1 of the other post"
    assert elsewhere.id in {o for o, _, _ in await rows("all_fields")}
    assert len(await rows("all_fields", include_deleted=True)) == 5
    high_band = await rows("rc_freq_part", frequency_center_from=Decimal("300"))
    assert [Decimal(m) for _, _, m in high_band] == [Decimal("300")]
