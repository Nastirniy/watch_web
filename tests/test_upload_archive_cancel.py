"""An archive cancels every pending upload of what it archives (owner decisions 2026-10-29, audit L6).

Archiving an observation, a post or a unit with its posts (cascade) ends every open resumable upload session of the
affected observations as aborted in the same transaction (the part file removed after the commit). A chunk, a
complete or a create for an archived observation answers 404 observation_not_found. Create and complete begin their
write with the parent row locks, and the transaction that inserts a media row begins with the post and observation
locks (MariaDB error 1020 on those inserts while an archive ran)."""
import pytest
from sqlalchemy import event
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.audit import AuditEvent
from app.backend.enums.routes import BrigadeRoutes, MediaRoutes, ObservationRoutes, PostRoutes
from app.backend.errors.coded import CodedError
from app.backend.models.audit import AuditLog
from app.backend.models.observation import Observation
from app.backend.models.upload_session import UploadSession
from app.backend.services.media.manager import get_media_manager
from tests.helpers import make_admin, make_dictionary, make_group, make_observation, make_post
from tests.test_media_uploads import CHUNK, _create_body, make_png, storage  # noqa: F401

CREATE = V1.path(MediaRoutes.UPLOADS)


async def _observation(db, admin, post):
    ids = await make_dictionary(db)
    return await make_observation(db, admin, post, ids["signal_type_id"])


async def _open_session(client, headers, admin, observation, data, sent: int):
    """An upload session of the observation with `sent` bytes received; its id"""
    created = await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(admin))
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    if sent:
        chunk = await client.patch(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id), content=data[:sent],
                                   headers={**CHUNK, "Upload-Offset": "0", **headers(admin)})
        assert chunk.status_code == 204, chunk.text
    return upload_id


async def _stored(db, upload_id) -> UploadSession:
    return (await db.exec(select(UploadSession).where(UploadSession.id == upload_id)
                          .execution_options(populate_existing=True))).one()


def _part(storage, upload_id):
    return storage / "_tmp" / "uploads" / f"{upload_id}.part"


async def _assert_cancelled(client, headers, db, storage, admin, upload_id, data, code):
    row = await _stored(db, upload_id)
    assert (row.status, row.error, row.writer_token) == ("aborted", code, None)
    assert not _part(storage, upload_id).exists(), "the part file is removed after the commit"
    state = await client.get(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id), headers=headers(admin))
    assert state.json()["status"] == "aborted" and state.json()["error"] == code, state.text
    chunk = await client.patch(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id), content=data[10:20],
                               headers={**CHUNK, "Upload-Offset": "10", **headers(admin)})
    assert chunk.status_code == 410 and chunk.json()["session_status"] == "aborted", chunk.text
    complete = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id),
                                 headers=headers(admin))
    assert complete.status_code == 410 and complete.json()["session_status"] == "aborted", complete.text


async def test_an_observation_archive_cancels_its_uploads(client, db, headers, storage):
    admin = await make_admin(db)
    post = await make_post(db)
    observation, other = await _observation(db, admin, post), await _observation(db, admin, post)
    data = make_png(200, 150)
    pending = await _open_session(client, headers, admin, observation, data, 10)
    fresh = await _open_session(client, headers, admin, observation, data, 0)
    elsewhere = await _open_session(client, headers, admin, other, data, 10)

    archived = await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=observation.id),
                                 headers=headers(admin))
    assert archived.status_code == 200, archived.text

    for upload_id in (pending, fresh):
        await _assert_cancelled(client, headers, db, storage, admin, upload_id, data, "observation_not_found")
    assert (await _stored(db, elsewhere)).status == "open" and _part(storage, elsewhere).exists()
    aborts = (await db.exec(select(AuditLog).where(AuditLog.event == AuditEvent.UPLOAD_SESSION_ABORT.code))).all()
    assert sorted(row.params["upload_id"] for row in aborts) == sorted([pending, fresh])

    # A new session for the archived observation: 404 as before
    refused = await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(admin))
    assert refused.status_code == 404 and refused.json()["code"] == "observation_not_found", refused.text


async def test_a_post_archive_cancels_the_uploads_of_its_observations(client, db, headers, storage):
    admin = await make_admin(db)
    post, other_post = await make_post(db), await make_post(db)
    observation, other = await _observation(db, admin, post), await _observation(db, admin, other_post)
    data = make_png(200, 150)
    pending = await _open_session(client, headers, admin, observation, data, 10)
    elsewhere = await _open_session(client, headers, admin, other, data, 10)

    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    await _assert_cancelled(client, headers, db, storage, admin, pending, data, "post_archived")
    assert (await _stored(db, elsewhere)).status == "open"


@pytest.mark.parametrize("cascade", [True, False])
async def test_a_unit_archive_cancels_uploads_only_when_it_archives_the_posts(client, db, headers, storage, cascade):
    admin = await make_admin(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    observation = await _observation(db, admin, post)
    data = make_png(200, 150)
    pending = await _open_session(client, headers, admin, observation, data, 10)

    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                                 params={"cascade": str(cascade).lower()}, headers=headers(admin))
    assert archived.status_code == 200, archived.text
    if cascade:
        await _assert_cancelled(client, headers, db, storage, admin, pending, data, "post_archived")
    else:
        assert (await _stored(db, pending)).status == "open", "the post moved, nothing was archived"


async def _archive_directly(db, observation):
    """An observation archived without the route (before this change, or a restored backup): its session stays open"""
    row = (await db.exec(select(Observation).where(Observation.id == observation.id))).one()
    row.is_deleted = True
    db.add(row)
    await db.commit()


async def test_chunk_and_complete_refuse_an_archived_observation(client, db, headers, storage):
    admin = await make_admin(db)
    post = await make_post(db)
    observation = await _observation(db, admin, post)
    data = make_png(200, 150)
    partial = await _open_session(client, headers, admin, observation, data, 10)
    full = await _open_session(client, headers, admin, observation, data, len(data))
    await _archive_directly(db, observation)

    chunk = await client.patch(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=partial), content=data[10:],
                               headers={**CHUNK, "Upload-Offset": "10", **headers(admin)})
    assert chunk.status_code == 404 and chunk.json()["code"] == "observation_not_found", chunk.text
    assert (await _stored(db, partial)).received_bytes == 10
    complete = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=full), headers=headers(admin))
    assert complete.status_code == 404 and complete.json()["code"] == "observation_not_found", complete.text
    assert (await _stored(db, full)).status == "open"


async def test_the_media_insert_refuses_an_archived_observation(db, storage, tmp_path):
    """A completion that claimed its session before the archive (a running completion is not cancelled) stores
    nothing: the insert transaction locks the observation and finds it archived"""
    admin = await make_admin(db)
    post = await make_post(db)
    observation = await _observation(db, admin, post)
    await _archive_directly(db, observation)
    source = tmp_path / "photo.png"
    source.write_bytes(make_png())
    with pytest.raises(CodedError) as caught:
        await get_media_manager().store_file_from_path(
            session=db, observation_id=observation.id, source_path=str(source), original_filename="photo.png",
            media_type="image", user_id=admin.id)
    assert caught.value.code.value == "observation_not_found"


# ------------------------------------------------------------------ lock order

def _statements(engine):
    seen = []

    def execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    def commit(conn):
        seen.append("COMMIT")

    event.listen(engine.sync_engine, "before_cursor_execute", execute)
    event.listen(engine.sync_engine, "commit", commit)

    def stop():
        event.remove(engine.sync_engine, "before_cursor_execute", execute)
        event.remove(engine.sync_engine, "commit", commit)
    return seen, stop


def _write_of(seen, marker):
    at = next(i for i, statement in enumerate(seen) if statement.startswith(marker))
    begin = max((i for i in range(at) if seen[i] == "COMMIT"), default=-1) + 1
    end = next((i for i in range(at, len(seen)) if seen[i] == "COMMIT"), len(seen))
    return seen[begin:end]


async def test_create_begins_with_the_post_and_the_observation(client, db, headers, storage, engine):
    admin = await make_admin(db)
    post = await make_post(db)
    observation = await _observation(db, admin, post)
    seen, stop = _statements(engine)
    try:
        created = await client.post(CREATE, json=_create_body(observation.id, make_png()), headers=headers(admin))
    finally:
        stop()
    assert created.status_code == 201, created.text
    write = _write_of(seen, "INSERT INTO upload_sessions")
    assert write[0].startswith("SELECT posts.id, posts.is_deleted FROM posts"), write
    assert write[1].startswith("SELECT observations."), write


async def test_complete_begins_with_the_observation_and_the_insert_with_the_parents(client, db, headers, storage,
                                                                                    engine):
    admin = await make_admin(db)
    post = await make_post(db)
    observation = await _observation(db, admin, post)
    data = make_png(200, 150)
    upload_id = await _open_session(client, headers, admin, observation, data, len(data))
    seen, stop = _statements(engine)
    try:
        done = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id),
                                 headers=headers(admin))
    finally:
        stop()
    assert done.status_code == 200, done.text
    claim = _write_of(seen, "UPDATE upload_sessions SET status=?")
    assert claim[0].startswith("SELECT observations.id, observations.is_deleted FROM observations"), claim
    assert claim[1].startswith("SELECT upload_sessions."), claim
    insert = _write_of(seen, "INSERT INTO media_files")
    assert insert[0].startswith("SELECT posts.id, posts.is_deleted FROM posts"), insert
    assert insert[1].startswith("SELECT observations.id, observations.is_deleted FROM observations"), insert
