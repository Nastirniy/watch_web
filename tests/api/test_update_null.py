"""Update models refuse an explicit null for a NOT NULL column (422 value_null), nullable columns keep accepting it,
and POST /observations/create without observed_at uses the current UTC time."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import (
    CategoryRoutes, MediaFormatRoutes, ObservationRoutes, SignalTypeRoutes, TagRoutes,
)
from app.backend.models.media_format import MediaFormat
from app.backend.models.observation import ObservationFrequency
from tests.helpers import make_admin, make_dictionary, make_observation, make_post, observation_payload

NOT_NULL = [
    ("observation", "signal_type_id"), ("observation", "observed_at"), ("observation", "confidence_level"),
    ("tag", "name"),
    ("category", "name"), ("category", "alias"),
    ("signal_type", "code"), ("signal_type", "name"), ("signal_type", "is_active"), ("signal_type", "sort_order"),
    ("media_format", "media_type"), ("media_format", "content_type"), ("media_format", "label"),
    ("media_format", "extensions"), ("media_format", "aliases"), ("media_format", "max_file_size_bytes"),
    ("media_format", "is_active"), ("media_format", "sort_order"),
    ("frequency", "kind"), ("frequency", "frequency_min"), ("frequency", "frequency_max"),
    ("frequency", "frequency_center"),
]

NULLABLE = [
    ("observation", "duration_seconds"), ("observation", "comment"), ("observation", "tag_ids"),
    ("signal_type", "category_id"), ("signal_type", "description"),
    ("frequency", "link_group"),
]


async def target(db, kind):
    """URL of an existing row of the kind"""
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    if kind == "tag":
        return admin, V1.path(TagRoutes.TAG_BY_ID, tag_id=ids["tag_id"])
    if kind == "category":
        return admin, V1.path(CategoryRoutes.CATEGORY_BY_ID, category_id=ids["category_id"])
    if kind == "signal_type":
        return admin, V1.path(SignalTypeRoutes.SIGNAL_TYPE_BY_ID, signal_type_id=ids["signal_type_id"])
    if kind == "media_format":
        row = (await db.exec(select(MediaFormat))).first()
        return admin, V1.path(MediaFormatRoutes.FORMAT_BY_ID, format_id=row.id)
    post = await make_post(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    if kind == "observation":
        return admin, V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=observation.id)
    band = (await db.exec(select(ObservationFrequency).where(ObservationFrequency.observation_id == observation.id))).first()
    return admin, V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID,
                          observation_id=observation.id, frequency_id=band.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,field", NOT_NULL)
async def test_explicit_null_for_not_null_column_is_refused(client, db, headers, kind, field):
    admin, url = await target(db, kind)
    response = await client.put(url, headers=headers(admin), json={field: None})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["code"] == "value_null"
    assert body["errors"][0]["pointer"] == f"#/{field}"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,field", NULLABLE)
async def test_explicit_null_for_nullable_column_is_accepted(client, db, headers, kind, field):
    admin, url = await target(db, kind)
    response = await client.put(url, headers=headers(admin), json={field: None})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_create_without_observed_at_uses_current_time(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    payload = observation_payload(post.id, ids["signal_type_id"])
    del payload["observed_at"]
    before = datetime.now(timezone.utc)
    response = await client.post(V1.path(ObservationRoutes.CREATE), headers=headers(admin), json=payload)
    assert response.status_code == 200, response.text
    observed = datetime.fromisoformat(response.json()["observed_at"])
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    assert before - timedelta(seconds=1) <= observed <= datetime.now(timezone.utc) + timedelta(seconds=1)
