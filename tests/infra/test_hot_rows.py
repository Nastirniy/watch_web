"""A request never writes its own user's row on a hot path.

Two requests of one user at the same moment (two devices, a double submit, an offline queue flushed after a
reconnect) collided on MariaDB 11.6+ whenever one of them wrote the users row: the other one's foreign key checks and
writes met a row changed after its snapshot (error 1020, 500), and two logins of one user deadlocked (1213). The live
test of 2026-10-19 saw 7 of 8 parallel observation creates of one user answer 500. users.last_login_at is now the
latest login session (derived, like presence), and nothing records the last observation on the user.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, inspect as sa_inspect

from app.backend.api.v1 import V1
from app.backend.auth.service import AuthService
from app.backend.enums.routes import ObservationRoutes, UserRoutes
from app.backend.models.user import User, UserSession
from tests.helpers import make_admin, make_dictionary, make_post, make_user, observation_payload

CREATE = V1.path(ObservationRoutes.CREATE)
USERS = V1.path(UserRoutes.ROOT)
USER = V1.path(UserRoutes.USER_BY_ID)


def users_writes(engine):
    """The UPDATE / INSERT / DELETE statements on the users table executed while the returned list is open"""
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        head = " ".join(statement.split()).upper()
        if head.startswith(("UPDATE USERS ", "INSERT INTO USERS ", "DELETE FROM USERS ")):
            seen.append(head[:80])

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine.sync_engine, "before_cursor_execute", before)


def test_users_has_no_hot_columns():
    columns = {column.name for column in User.__table__.columns}
    assert "last_login_at" not in columns and "last_record_created_at" not in columns
    assert "last_login_at" in {prop.key for prop in sa_inspect(User).column_attrs}, "still readable, derived"


@pytest.mark.asyncio
async def test_observation_create_does_not_write_the_users_row(client, db, engine, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.create"], post_id=post.id)
    auth = headers(user)
    seen, stop = users_writes(engine)
    try:
        response = await client.post(CREATE, json=observation_payload(post.id, ids["signal_type_id"]), headers=auth)
    finally:
        stop()
    assert response.status_code == 200, response.text
    assert seen == []


@pytest.mark.asyncio
async def test_login_does_not_write_the_users_row(db, engine):
    user = await make_user(db)
    seen, stop = users_writes(engine)
    try:
        await AuthService.create_session(db, user)
    finally:
        stop()
    assert seen == []


@pytest.mark.asyncio
async def test_last_login_at_is_the_latest_login_session(client, db, headers):
    admin = await make_admin(db)
    auth = headers(admin)
    never = await make_user(db, username="never_logged")
    twice = await make_user(db, username="twice_logged")
    early, late = datetime(2026, 9, 20, 7, 0), datetime(2026, 9, 21, 8, 30)
    for created in (early, late):
        db.add(UserSession(id=f"s-{created:%d%H%M}", user_id=twice.id, created_at=created, last_activity=created,
                           expires_at=created + timedelta(minutes=30), ended_at=created + timedelta(minutes=30),
                           end_reason="logout"))
    await db.commit()

    body = (await client.get(USER.format(user_id=twice.id), headers=auth)).json()
    assert body["last_login_at"] == "2026-09-21T08:30:00Z", "ended sessions count: a login happened"
    assert (await client.get(USER.format(user_id=never.id), headers=auth)).json()["last_login_at"] is None

    listed = await client.get(USERS, params={"sort_by": "last_login_at", "sort_order": "ascending",
                                             "last_login_from": "2026-09-21T00:00:00Z",
                                             "last_login_to": "2026-09-22T00:00:00Z"}, headers=auth)
    assert listed.status_code == 200, listed.text
    assert [u["username"] for u in listed.json()] == ["twice_logged"]


@pytest.mark.asyncio
async def test_upload_writes_its_media_row_as_the_worker_left_it(client, db, headers, session_maker, monkeypatch, tmp_path):
    """A worker could claim the poster of a new video while the upload request still probed it. The request then wrote
    a media_files row read before that claim: error 1020 on MariaDB (1 complete of 8 answered 500 in the video stress
    test of 2026-10-19). The hold now lasts until the request's last transaction, so only a hold that ran out (a
    request stalled for jobs.UPLOAD_HOLD) lets a worker in: the request locks the row after probing and writes the
    committed copy, the audit row records the worker's claim."""
    from app.backend.api.shared.helpers import upload_helper
    from app.backend.config import settings
    from app.backend.models.audit import AuditLog
    from app.backend.services.media import jobs
    from app.backend.services.media.manager import reset_media_manager
    from app.backend.enums.routes import MediaRoutes
    from sqlmodel import select
    from tests.media.test_poster_jobs import MP4_BYTES

    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()

    async def probe_while_the_worker_claims(media_file):
        later = datetime.now(timezone.utc) + jobs.UPLOAD_HOLD + timedelta(seconds=1)
        async with session_maker() as worker:
            assert await jobs.claim_next_poster(worker, "w1") is None, "held while the request probes"
            assert await jobs.claim_next_poster(worker, "w1", lease_seconds=300, now=later) is not None
        return {"duration_seconds": 3.0}

    monkeypatch.setattr(upload_helper, "_probe_headers", probe_while_the_worker_claims)
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create", "observation.view"], post_id=post.id)
    from tests.helpers import make_observation
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    try:
        response = await client.post(
            V1.path(MediaRoutes.UPLOAD), data={"observation_id": observation.id, "media_type": "video"},
            files={"file": ("clip.mp4", MP4_BYTES, "video/mp4")}, headers=headers(user),
        )
    finally:
        reset_media_manager()
    assert response.status_code in (200, 201), response.text
    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "media", AuditLog.action == "create"))).one()
    assert audit.new_values["poster_status"] == "processing" and audit.new_values["duration_seconds"] == 3.0
