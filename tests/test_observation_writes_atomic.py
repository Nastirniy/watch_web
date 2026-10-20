"""Observation updates and band writes are one transaction: the change and its audit row are committed together.

Each route committed its change and wrote the audit row in a second transaction: a failure there (on MariaDB the audit
row's foreign key check of the caller's users row answers 1020 after the refresh opened a read view; a lock wait
timeout; a lost connection) answered 500 for a stored change. A client retrying the 500 of POST .../frequencies added
a second band; the audit row was missing in every case."""
from decimal import Decimal

import pytest
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import ObservationRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.observation import Observation, ObservationFrequency
from tests.helpers import make_dictionary, make_observation, make_post, make_user

OBSERVATION = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
BANDS = V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES)
BAND = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID)
BAND_ARCHIVE = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE)
BAND_RESTORE = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE)
OPERATOR = ["observation.view", "observation.create", "observation.update", "observation.archive"]


async def _audit_fails(*args, **kwargs):
    raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed since last read')"))


async def _setup(db, session_maker):
    post = await make_post(db)
    dictionary = await make_dictionary(db)
    user = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, user, post, dictionary["signal_type_id"],
                                         bands=[("rc", "100.000", "200.000"), ("video", "5720", "5800")])
    async with session_maker() as fresh:
        first, second = (await fresh.exec(select(ObservationFrequency.id).where(
            ObservationFrequency.observation_id == observation.id).order_by(ObservationFrequency.id))).all()
    return user, observation, first, second


async def _bands(session_maker, observation_id):
    async with session_maker() as fresh:
        rows = (await fresh.exec(select(ObservationFrequency).where(
            ObservationFrequency.observation_id == observation_id).order_by(ObservationFrequency.id))).all()
        return [(r.id, r.kind, r.frequency_min, r.frequency_max, r.link_group, r.is_deleted) for r in rows]


async def _audit_count(session_maker, resource_type):
    async with session_maker() as fresh:
        return (await fresh.exec(select(func.count()).select_from(AuditLog).where(
            AuditLog.resource_type == resource_type))).one()


def _calls(client, headers, user, observation_id, second):
    """name -> (request, audit resource_type, audit action)"""
    auth = headers(user)
    return {
        "add_band": (lambda: client.post(
            BANDS.format(observation_id=observation_id), headers=auth,
            json={"kind": "rc", "frequency_min": "300", "frequency_max": "400"}), "observation_frequency", "create"),
        "update_band": (lambda: client.put(
            BAND.format(observation_id=observation_id, frequency_id=second), headers=auth,
            json={"frequency_max": "5900", "link_group": 2}), "observation_frequency", "update"),
        "archive_band": (lambda: client.post(
            BAND_ARCHIVE.format(observation_id=observation_id, frequency_id=second), headers=auth),
            "observation_frequency", "archive"),
        "update_observation": (lambda: client.put(
            OBSERVATION.format(observation_id=observation_id), headers=auth, json={"duration_seconds": 42}),
            "observation", "update"),
    }


ROUTES = ["add_band", "update_band", "archive_band", "update_observation"]


@pytest.mark.parametrize("name", ROUTES)
async def test_a_failed_audit_row_stores_no_change(client, db, headers, session_maker, monkeypatch, name):
    from app.backend.api.v1 import observations as observations_module
    user, observation, first, second = await _setup(db, session_maker)
    call, resource_type, _ = _calls(client, headers, user, observation.id, second)[name]
    bands_before = await _bands(session_maker, observation.id)
    audits_before = await _audit_count(session_maker, resource_type)
    async with session_maker() as fresh:
        duration_before = (await fresh.get(Observation, observation.id)).duration_seconds

    monkeypatch.setattr(observations_module, "create_audit_log", _audit_fails)
    response = await call()
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text

    assert await _bands(session_maker, observation.id) == bands_before
    assert await _audit_count(session_maker, resource_type) == audits_before
    async with session_maker() as fresh:
        assert (await fresh.get(Observation, observation.id)).duration_seconds == duration_before


async def test_a_failed_audit_row_stores_no_restore(client, db, headers, session_maker, monkeypatch):
    from app.backend.api.v1 import observations as observations_module
    user, observation, first, second = await _setup(db, session_maker)
    archived = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=second),
                                 headers=headers(user))
    assert archived.status_code == 200, archived.text
    bands_before = await _bands(session_maker, observation.id)
    audits_before = await _audit_count(session_maker, "observation_frequency")

    monkeypatch.setattr(observations_module, "create_audit_log", _audit_fails)
    response = await client.post(BAND_RESTORE.format(observation_id=observation.id, frequency_id=second),
                                 headers=headers(user))
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    assert await _bands(session_maker, observation.id) == bands_before
    assert await _audit_count(session_maker, "observation_frequency") == audits_before


@pytest.mark.parametrize("name", ROUTES)
async def test_the_change_and_one_audit_row_are_stored(client, db, headers, session_maker, name):
    user, observation, first, second = await _setup(db, session_maker)
    call, resource_type, action = _calls(client, headers, user, observation.id, second)[name]
    response = await call()
    assert response.status_code == 200, response.text
    async with session_maker() as fresh:
        (audit,) = (await fresh.exec(select(AuditLog).where(
            AuditLog.resource_type == resource_type, AuditLog.action == action))).all()
    assert audit.user_id == user.id
    bands = await _bands(session_maker, observation.id)
    if name == "add_band":
        assert len(bands) == 3 and audit.resource_id == response.json()["id"]
        assert audit.new_values["id"] == response.json()["id"] and audit.new_values["observation_id"] == observation.id
    elif name == "update_band":
        assert audit.resource_id == second and audit.old_values["frequency_max"] != audit.new_values["frequency_max"]
        assert Decimal(response.json()["frequency_max"]) == Decimal("5900") and response.json()["link_group"] == 2
    elif name == "archive_band":
        assert audit.resource_id == second and audit.new_values["is_deleted"] is True
        assert [b[0] for b in bands if b[5]] == [second]
    else:
        assert audit.resource_id == observation.id
        assert audit.old_values["duration_seconds"] is None and audit.new_values["duration_seconds"] == 42


async def test_restore_writes_one_audit_row(client, db, headers, session_maker):
    user, observation, first, second = await _setup(db, session_maker)
    auth = headers(user)
    archived = await client.post(BAND_ARCHIVE.format(observation_id=observation.id, frequency_id=second), headers=auth)
    assert archived.status_code == 200, archived.text
    restored = await client.post(BAND_RESTORE.format(observation_id=observation.id, frequency_id=second), headers=auth)
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    async with session_maker() as fresh:
        (audit,) = (await fresh.exec(select(AuditLog).where(
            AuditLog.resource_type == "observation_frequency", AuditLog.action == "restore"))).all()
    assert audit.resource_id == second and audit.new_values["is_deleted"] is False
