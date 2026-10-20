"""POST /observations/ is one transaction: the observation, its bands, tags, unit history and its audit row are
committed together.

The audit row was written in a second transaction after the observation's commit: any failure there (on MariaDB the
audit row's foreign key check of the caller's users row answers 1020 when an administrator changed that row after the
second read view; a lock wait timeout; a lost connection) answered 500 for an observation that was stored, so a
client retrying the 500 created it twice and the audit row was missing."""
from sqlalchemy import event, func
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import ObservationRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.observation import Observation, ObservationFrequency
from tests.helpers import make_admin, make_dictionary, make_post, observation_payload

CREATE = V1.path(ObservationRoutes.CREATE)


def _statements(engine):
    """Statements executed while the returned list is open (whitespace folded), "COMMIT" / "ROLLBACK" where a
    transaction ended"""
    seen = []

    def execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    def commit(conn):
        seen.append("COMMIT")

    def rollback(conn):
        seen.append("ROLLBACK")

    listeners = (("before_cursor_execute", execute), ("commit", commit), ("rollback", rollback))
    for name, listener in listeners:
        event.listen(engine.sync_engine, name, listener)

    def stop():
        for name, listener in listeners:
            event.remove(engine.sync_engine, name, listener)
    return seen, stop


def _transaction_of(seen, marker):
    at = next(i for i, statement in enumerate(seen) if statement.startswith(marker))
    begin = max((i for i in range(at) if seen[i] in ("COMMIT", "ROLLBACK")), default=-1) + 1
    end = next((i for i in range(at, len(seen)) if seen[i] in ("COMMIT", "ROLLBACK")), len(seen))
    return seen[begin:end + 1]


async def test_a_failed_audit_row_stores_no_observation(client, db, headers, session_maker, monkeypatch):
    from app.backend.api.v1 import observations as observations_module
    admin = await make_admin(db)
    post = await make_post(db)
    dictionary = await make_dictionary(db)

    async def audit_fails(*args, **kwargs):
        raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed since last read')"))

    monkeypatch.setattr(observations_module, "create_audit_log", audit_fails)
    response = await client.post(CREATE, headers=headers(admin),
                                 json=observation_payload(post.id, dictionary["signal_type_id"]))
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    async with session_maker() as fresh:
        observations = (await fresh.exec(select(func.count()).select_from(Observation))).one()
        bands = (await fresh.exec(select(func.count()).select_from(ObservationFrequency))).one()
    assert (observations, bands) == (0, 0)


async def test_the_audit_row_is_committed_with_the_observation(client, db, headers, engine, session_maker):
    admin = await make_admin(db)
    post = await make_post(db)
    dictionary = await make_dictionary(db)
    auth_header = headers(admin)
    seen, stop = _statements(engine)
    try:
        response = await client.post(CREATE, headers=auth_header,
                                     json=observation_payload(post.id, dictionary["signal_type_id"]))
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _transaction_of(seen, "INSERT INTO observations")
    assert write[-1] == "COMMIT", write
    assert any(statement.startswith("INSERT INTO audit_logs") for statement in write), write
    created = response.json()
    async with session_maker() as fresh:
        (audit,) = (await fresh.exec(select(AuditLog).where(AuditLog.action == "create",
                                                            AuditLog.resource_type == "observation"))).all()
    assert audit.resource_id == created["id"] and audit.user_id == admin.id
    assert audit.new_values["id"] == created["id"] and audit.new_values["post_id"] == post.id
    (band,) = audit.new_values["frequencies"]
    assert band["id"] == created["frequencies"][0]["id"] and float(band["frequency_min"]) == 100.0
