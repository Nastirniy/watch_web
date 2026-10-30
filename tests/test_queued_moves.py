"""Queued moves (spec 2026-10-30): a move with uploads running below the node answers 202 and waits; new uploads below
it answer 409 move_pending, other writes go on; an upload silent for moves.upload_stale_seconds is cancelled while the
move waits; force runs it at once; the requester, or anyone who could make the same move, cancels it; the permission
checks run again when it runs, and a mover who lost the right ends it failed with nothing moved.

The SQLite tests cannot race: the concurrent cases run against MariaDB (tests/live/moves_races.py is not part of the
suite)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.enums.audit import AuditEvent
from app.backend.enums.general import BuiltinRole, Permissions as P, UserType
from app.backend.enums.routes import (
    BrigadeRoutes, MediaRoutes, MoveRoutes, ObservationRoutes, PostRoutes, ScopeGroupRoutes,
)
from app.backend.models.audit import AuditLog
from app.backend.models.pending_move import PendingMove
from app.backend.models.post import Post
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.upload_session import UploadSession
from app.backend.services import moves as moves_service
from app.backend.workers.media_worker import MediaWorker
from tests import helpers as factories
from tests.helpers import builtin_role, give, make_admin, make_group, make_post, make_user
from tests.test_media_uploads import CHUNK, _create_body, make_png, storage  # noqa: F401
from tests.test_upload_archive_cancel import CREATE, _observation, _open_session, _part, _stored

MOVER = [P.POST_VIEW, P.POST_UPDATE, P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_UPDATE]
DATA = make_png(200, 150)


def _post_url(post) -> str:
    return V1.path(PostRoutes.POST_BY_ID, post_id=post.id)


def _session_url(upload_id: str) -> str:
    return V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id)


def _complete_url(upload_id: str) -> str:
    return V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id)


def _move_url(move_id: int) -> str:
    return V1.path(MoveRoutes.MOVE_BY_ID, move_id=move_id)


def _cancel_url(move_id: int) -> str:
    return V1.path(MoveRoutes.MOVE_CANCEL, move_id=move_id)


async def _group_of(db, post) -> int:
    return (await db.exec(select(Post.group_id).where(Post.id == post.id)
                          .execution_options(populate_existing=True))).one()


async def _parent_of(db, group) -> int:
    return (await db.exec(select(ScopeGroup.parent_id).where(ScopeGroup.id == group.id)
                          .execution_options(populate_existing=True))).one()


async def _move(db, move_id: int) -> PendingMove:
    return (await db.exec(select(PendingMove).where(PendingMove.id == move_id)
                          .execution_options(populate_existing=True))).one()


async def _events(db, event: AuditEvent) -> list:
    return list((await db.exec(select(AuditLog).where(AuditLog.event == event.code).order_by(AuditLog.id))).all())


async def _chunk(client, headers, user, upload_id: str, offset: int, size: int):
    return await client.patch(_session_url(upload_id), content=DATA[offset:offset + size],
                              headers={**CHUNK, "Upload-Offset": str(offset), **headers(user)})


async def _send_rest(client, headers, user, upload_id: str, offset: int):
    chunk = await _chunk(client, headers, user, upload_id, offset, len(DATA) - offset)
    assert chunk.status_code == 204, chunk.text


async def _age(db, upload_id: str, seconds: int = 3600) -> None:
    """The last chunk of the session lies this far back"""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await db.execute(update(UploadSession).where(UploadSession.id == upload_id).values(last_chunk_at=moment))
    await db.commit()


def _worker(session_maker, storage) -> MediaWorker:
    return MediaWorker(session_maker, storage, name="media@test:1", extractor=lambda source, target: None,
                       storyboard_extractor=lambda *args: None)


async def _mover_at_root(db):
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, user, MOVER)
    return user


class _Scene:
    """An administrator, two groups, a post in the first with an observation and an upload of 10 bytes running"""

    def __init__(self, admin, home, there, post, observation, upload_id):
        self.admin, self.home, self.there, self.post = admin, home, there, post
        self.observation, self.upload_id = observation, upload_id


async def _scene(client, db, headers, sent: int = 10) -> _Scene:
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    observation = await _observation(db, admin, post)
    upload_id = await _open_session(client, headers, admin, observation, DATA, sent)
    return _Scene(admin, home, there, post, observation, upload_id)


async def _waiting_move(client, headers, scene) -> int:
    answer = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text
    return answer.json()["id"]


# ---------------------------------------------------------------- no upload below the node: as before

async def test_a_move_without_uploads_runs_at_once_and_leaves_no_row(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    moved = await client.put(_post_url(post), json={"group_id": there.id}, headers=headers(admin))
    assert moved.status_code == 200 and moved.json()["id"] == post.id, moved.text
    assert await _group_of(db, post) == there.id
    assert (await db.exec(select(PendingMove))).all() == []


async def test_a_finished_or_aborted_upload_holds_nothing(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    moved = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(scene.admin))
    assert moved.status_code == 200, moved.text


# ---------------------------------------------------------------- 202 and the operation

async def test_a_post_move_with_an_upload_waits(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    answer = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert answer.headers["Location"] == _move_url(body["id"])
    assert answer.headers["Retry-After"] == str(settings.moves.check_interval_seconds)
    assert body["status"] == "waiting" and body["node_kind"] == "post" and body["node_id"] == scene.post.id
    assert body["target_group_id"] == scene.there.id and body["force"] is False
    assert await _group_of(db, scene.post) == scene.home.id, "nothing moves yet"

    state = await client.get(_move_url(body["id"]), headers=headers(scene.admin))
    assert state.status_code == 200, state.text
    assert state.json()["waiting_seconds"] >= 0 and state.json()["requested_at"]
    (upload,) = state.json()["uploads"]
    assert upload["upload_id"] == scene.upload_id and upload["post_id"] == scene.post.id
    assert upload["received_bytes"] == 10 and upload["total_bytes"] == len(DATA)
    assert upload["last_chunk_at"] and upload["holding"] is True

    (requested,) = await _events(db, AuditEvent.MOVE_REQUESTED)
    assert requested.params["move_id"] == body["id"] and requested.user_id == scene.admin.id
    assert requested.params["node_name"] == scene.post.name and requested.params["target_group_id"] == scene.there.id


async def test_a_group_move_waits_for_an_upload_of_any_post_below_it(client, db, headers, storage):
    admin = await make_admin(db)
    top, middle, there = await make_group(db), await make_group(db), await make_group(db)
    await db.execute(update(ScopeGroup).where(ScopeGroup.id == middle.id).values(parent_id=top.id, parent_key=top.id))
    await db.commit()
    post, beside = await make_post(db, group_id=middle.id), await make_post(db, group_id=top.id)
    observation = await _observation(db, admin, post)
    await _open_session(client, headers, admin, observation, DATA, 10)

    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id), json={"parent_id": there.id},
                                headers=headers(admin))
    assert answer.status_code == 202 and answer.json()["node_kind"] == "group", answer.text
    assert await _parent_of(db, top) is None
    # another post below the node refuses new uploads as well, a post elsewhere does not
    refused = await client.post(CREATE, json=_create_body((await _observation(db, admin, beside)).id, DATA),
                                headers=headers(admin))
    assert refused.status_code == 409 and refused.json()["code"] == "move_pending", refused.text


async def test_a_brigade_move_waits_and_runs_when_the_upload_ends(client, db, headers, storage):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)

    answer = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                json={"corps_id": other.id}, headers=headers(admin))
    assert answer.status_code == 202 and answer.json()["target_group_id"] == other.id, answer.text
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(admin))
    assert (await _move(db, answer.json()["id"])).status == "done"
    assert await _parent_of(db, brigade) == other.id


# ---------------------------------------------------------------- what is blocked

@pytest.mark.parametrize("route", ["resumable", "multipart", "nginx"])
async def test_new_uploads_answer_move_pending(client, db, headers, storage, route, monkeypatch, tmp_path):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    if route == "resumable":
        refused = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    elif route == "multipart":
        refused = await client.post(V1.path(MediaRoutes.UPLOAD), headers=headers(scene.admin),
                                    data={"observation_id": scene.observation.id, "media_type": "image"},
                                    files={"file": ("a.png", DATA, "image/png")})
    else:
        monkeypatch.setattr(settings.storage, "enable_nginx", True)
        monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(tmp_path))
        body = tmp_path / "0000000001"
        body.write_bytes(DATA)
        refused = await client.post(
            V1.path(MediaRoutes.NGINX_UPLOAD), params={"observation_id": scene.observation.id, "media_type": "image"},
            headers={**headers(scene.admin), "X-File-Name": str(body), "X-Original-Filename": "a.png",
                     "Content-Type": "image/png"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "move_pending" and refused.json()["move_id"] == move_id
    assert refused.headers["Retry-After"] == str(settings.moves.check_interval_seconds)


async def test_an_upload_that_passed_the_first_check_is_refused_when_it_stores(client, db, headers, storage, session_maker):
    """A one-shot upload checks again under the post lock in the store transaction: the move that came while the body
    was spooled is seen. The resumable complete does not ask: running uploads continue."""
    from app.backend.errors.coded import CodedError
    from app.backend.services.media.manager import get_media_manager

    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    source = storage / "spooled.png"
    source.write_bytes(DATA)
    async with session_maker() as session:
        with pytest.raises(CodedError) as caught:
            await get_media_manager().store_file_from_path(
                session=session, observation_id=scene.observation.id, source_path=str(source),
                original_filename="a.png", media_type="image", user_id=scene.admin.id, refuse_waiting_move=True)
    assert caught.value.code.value == "move_pending" and caught.value.params == {"move_id": move_id}


async def test_uploads_elsewhere_and_other_writes_go_on(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    elsewhere = await _observation(db, scene.admin, await make_post(db))
    created = await client.post(CREATE, json=_create_body(elsewhere.id, DATA), headers=headers(scene.admin))
    assert created.status_code == 201, created.text
    renamed = await client.put(_post_url(scene.post), json={"name": "Still here"}, headers=headers(scene.admin))
    assert renamed.status_code == 200 and renamed.json()["name"] == "Still here", renamed.text


async def test_chunks_and_complete_continue_and_the_last_end_runs_the_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _send_rest(client, headers, scene.admin, scene.upload_id, 10)
    assert await _group_of(db, scene.post) == scene.home.id, "complete has not ended the upload yet"

    completed = await client.post(_complete_url(scene.upload_id), headers=headers(scene.admin))
    assert completed.status_code == 200, completed.text
    assert await _group_of(db, scene.post) == scene.there.id
    row = await _move(db, move_id)
    assert row.status == "done" and row.finished_at is not None and row.error is None
    (done,) = await _events(db, AuditEvent.MOVE_COMPLETED)
    assert done.params["move_id"] == move_id and "forced" not in done.params and done.user_id == scene.admin.id
    (update_row,) = await _events(db, AuditEvent.POST_UPDATE)
    assert update_row.old_values["group_id"] == scene.home.id and update_row.new_values["group_id"] == scene.there.id
    allowed = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert allowed.status_code == 201, "the block lifts with the move"


async def test_the_move_runs_when_the_last_of_two_uploads_ends(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    second = await _open_session(client, headers, scene.admin, scene.observation, DATA, 10)
    move_id = await _waiting_move(client, headers, scene)
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id), headers=headers(scene.admin))
    assert (await _move(db, move_id)).status == "waiting"
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=second), headers=headers(scene.admin))
    assert (await _move(db, move_id)).status == "done"


async def test_an_archive_of_the_observation_ends_the_upload_and_runs_the_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    archived = await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=scene.observation.id),
                                 headers=headers(scene.admin))
    assert archived.status_code == 200, archived.text
    assert (await _move(db, move_id)).status == "done"
    assert await _group_of(db, scene.post) == scene.there.id


# ---------------------------------------------------------------- the stale rule

async def test_a_stale_upload_is_cancelled_and_the_move_runs(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    state = await client.get(_move_url(move_id), headers=headers(scene.admin))
    assert state.json()["uploads"][0]["holding"] is False

    await _worker(session_maker, storage).check_moves()

    row = await _stored(db, scene.upload_id)
    assert (row.status, row.error, row.writer_token) == ("aborted", "upload_cancelled_by_move", None)
    assert not _part(storage, scene.upload_id).exists(), "the part file goes after the commit"
    assert (await _move(db, move_id)).status == "done"
    assert await _group_of(db, scene.post) == scene.there.id
    abort = (await _events(db, AuditEvent.UPLOAD_SESSION_ABORT))[-1]
    assert abort.params["upload_id"] == scene.upload_id and abort.new_values["error"] == "upload_cancelled_by_move"

    chunk = await _chunk(client, headers, scene.admin, scene.upload_id, 10, 10)
    assert chunk.status_code == 409 and chunk.json()["code"] == "upload_cancelled_by_move", chunk.text
    assert chunk.json()["session_status"] == "aborted"
    complete = await client.post(_complete_url(scene.upload_id), headers=headers(scene.admin))
    assert complete.status_code == 409 and complete.json()["code"] == "upload_cancelled_by_move", complete.text
    seen = await client.get(_session_url(scene.upload_id), headers=headers(scene.admin))
    assert seen.json()["status"] == "aborted" and seen.json()["error"] == "upload_cancelled_by_move"


async def test_an_upload_that_receives_bytes_holds_the_move(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).status == "open"
    assert (await _move(db, move_id)).status == "waiting"


async def test_a_session_that_never_received_a_chunk_counts_from_its_creation(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers, sent=0)
    assert (await _stored(db, scene.upload_id)).last_chunk_at is None
    move_id = await _waiting_move(client, headers, scene)
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "waiting"
    moment = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(created_at=moment))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).error == "upload_cancelled_by_move"


async def test_the_stale_rule_acts_only_while_a_move_waits(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    await _age(db, scene.upload_id)
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).status == "open"


async def test_a_completion_that_runs_holds_the_move(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    now = datetime.now(timezone.utc)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(
        status="completing", writer_token="t" * 32, writer_since=now, last_chunk_at=now - timedelta(hours=1)))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).status == "completing"
    assert (await _move(db, move_id)).status == "waiting"


async def test_an_expired_session_no_longer_holds_the_move(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    moment = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(expires_at=moment))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).status == "expired"
    assert (await _move(db, move_id)).status == "done"


async def test_last_chunk_at_moves_with_bytes_only(client, db, headers, storage):
    scene = await _scene(client, db, headers, sent=0)
    row = await _stored(db, scene.upload_id)
    assert row.last_chunk_at is None
    empty_claim = await client.patch(_session_url(scene.upload_id), content=b"",
                                     headers={**CHUNK, "Upload-Offset": "0", **headers(scene.admin)})
    assert empty_claim.status_code == 400
    assert (await _stored(db, scene.upload_id)).last_chunk_at is None
    wrong_offset = await _chunk(client, headers, scene.admin, scene.upload_id, 5, 5)
    assert wrong_offset.status_code == 409
    assert (await _stored(db, scene.upload_id)).last_chunk_at is None
    assert (await _chunk(client, headers, scene.admin, scene.upload_id, 0, 5)).status_code == 204
    assert (await _stored(db, scene.upload_id)).last_chunk_at is not None


async def test_a_worker_tick_recovers_a_move_nothing_holds(client, db, headers, storage, session_maker):
    """The crash between the end of the last upload and the run: the worker finishes it"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(status="completed"))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "done"
    assert await _group_of(db, scene.post) == scene.there.id


async def test_the_worker_loop_keeps_going_and_runs_the_check(client, db, headers, storage, session_maker, monkeypatch):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    worker = _worker(session_maker, storage)
    original = worker.check_moves

    async def once():
        await original()
        worker._stop.set()

    monkeypatch.setattr(worker, "check_moves", once)
    await worker._moves_loop()
    assert (await _move(db, move_id)).status == "done"


# ---------------------------------------------------------------- one run only

async def test_two_runs_of_one_move_change_it_once(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(status="completed"))
    await db.commit()
    for _ in range(2):
        async with session_maker() as session:
            await moves_service.run_move(session, move_id)
    assert len(await _events(db, AuditEvent.MOVE_COMPLETED)) == 1
    assert len(await _events(db, AuditEvent.POST_UPDATE)) == 1


async def test_a_run_with_an_upload_still_pending_changes_nothing(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    async with session_maker() as session:
        await moves_service.run_move(session, move_id)
    assert (await _move(db, move_id)).status == "waiting"
    assert await _group_of(db, scene.post) == scene.home.id


# ---------------------------------------------------------------- force

@pytest.mark.parametrize("kind", ["post", "group", "brigade"])
async def test_force_runs_at_once_and_uploads_continue(client, db, headers, storage, kind):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    observation = await _observation(db, admin, post)
    upload_id = await _open_session(client, headers, admin, observation, DATA, 10)
    if kind == "post":
        moved = await client.put(_post_url(post), json={"group_id": other.id, "force": True}, headers=headers(admin))
        assert await _group_of(db, post) == other.id
    elif kind == "group":
        target = await make_group(db)
        plain = await make_group(db)
        await db.execute(update(Post).where(Post.id == post.id).values(group_id=plain.id))
        await db.commit()
        moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=plain.id),
                                   json={"parent_id": target.id, "force": True}, headers=headers(admin))
        assert await _parent_of(db, plain) == target.id
    else:
        moved = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                   json={"corps_id": other.id, "force": True}, headers=headers(admin))
        assert await _parent_of(db, brigade) == other.id
    assert moved.status_code == 200 and "force" not in moved.json(), moved.text

    (row,) = (await db.exec(select(PendingMove))).all()
    assert (row.status, row.force) == ("done", True)
    (done,) = await _events(db, AuditEvent.MOVE_COMPLETED)
    assert done.params["forced"] is True
    assert await _events(db, AuditEvent.MOVE_REQUESTED) == []

    await _send_rest(client, headers, admin, upload_id, 10)
    completed = await client.post(_complete_url(upload_id), headers=headers(admin))
    assert completed.status_code == 200, completed.text


async def test_force_without_uploads_is_a_plain_move(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    moved = await client.put(_post_url(post), json={"group_id": there.id, "force": True}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    assert (await db.exec(select(PendingMove))).all() == []


async def test_after_a_forced_move_complete_needs_the_right_at_the_new_place(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    uploader = await make_user(db, perms=[P.MEDIA_VIEW, P.MEDIA_CREATE], post_id=post.id)
    upload_id = await _open_session(client, headers, uploader, await _observation(db, admin, post), DATA, 10)
    await _send_rest(client, headers, uploader, upload_id, 10)
    # the grant lay on the group the post leaves
    grants = (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == uploader.id))).all()
    for grant in grants:
        await db.delete(grant)
    await db.commit()
    await give(db, uploader, [P.MEDIA_VIEW, P.MEDIA_CREATE], Node.group(home.id))

    moved = await client.put(_post_url(post), json={"group_id": there.id, "force": True}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    refused = await client.post(_complete_url(upload_id), headers=headers(uploader))
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------- a move is its own request

async def test_a_waiting_move_refuses_other_fields(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    refused = await client.put(_post_url(scene.post), json={"group_id": scene.there.id, "name": "New name"},
                               headers=headers(scene.admin))
    assert refused.status_code == 422 and refused.json()["code"] == "move_not_alone", refused.text
    assert refused.json()["changed_fields"] == ["name"]
    assert (await db.exec(select(PendingMove))).all() == []


async def test_fields_sent_with_their_current_value_do_not_count(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    answer = await client.put(_post_url(scene.post), json={"group_id": scene.there.id, "name": scene.post.name},
                              headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text


@pytest.mark.parametrize("body", [{"force": True}, {}])
async def test_a_move_with_other_fields_runs_as_before_when_it_need_not_wait(client, db, headers, storage, body):
    scene = await _scene(client, db, headers, sent=0)
    if not body:
        await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id), headers=headers(scene.admin))
    moved = await client.put(_post_url(scene.post), json={"group_id": scene.there.id, "name": "Moved", **body},
                             headers=headers(scene.admin))
    assert moved.status_code == 200 and moved.json()["name"] == "Moved", moved.text


async def test_a_group_rename_with_a_move_that_would_wait_is_refused(client, db, headers, storage):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    refused = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                 json={"corps_id": other.id, "name": "Other name"}, headers=headers(admin))
    assert refused.status_code == 422 and refused.json()["changed_fields"] == ["name"], refused.text


# ---------------------------------------------------------------- overlapping moves

async def test_moves_overlapping_a_waiting_one_answer_move_pending(client, db, headers, storage):
    admin = await make_admin(db)
    top, there = await make_group(db), await make_group(db)
    post, beside = await make_post(db, group_id=top.id), await make_post(db, group_id=top.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id), json={"parent_id": there.id},
                                headers=headers(admin))
    assert answer.status_code == 202, answer.text
    move_id = answer.json()["id"]

    for body, url, method in (({"group_id": there.id}, _post_url(beside), client.put),
                              ({"group_id": there.id, "force": True}, _post_url(post), client.put),
                              ({"parent_id": None}, V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id), client.patch)):
        refused = await method(url, json=body, headers=headers(admin))
        if body == {"parent_id": None}:
            assert refused.status_code == 200, "its own parent sent back is no move"
            continue
        assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text
        assert refused.headers["Retry-After"] == str(settings.moves.check_interval_seconds)

    second_there = await make_group(db)
    again = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id),
                               json={"parent_id": second_there.id}, headers=headers(admin))
    assert again.status_code == 409 and again.json()["move_id"] == move_id, again.text


async def test_a_move_of_a_group_above_a_waiting_post_move_answers_move_pending(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    refused = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=scene.home.id),
                                 json={"parent_id": scene.there.id}, headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text


async def test_a_move_elsewhere_is_not_blocked_by_a_waiting_one(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    other = await make_post(db, group_id=scene.there.id)
    moved = await client.put(_post_url(other), json={"group_id": scene.home.id}, headers=headers(scene.admin))
    assert moved.status_code == 200, moved.text


# ---------------------------------------------------------------- cancel

async def test_the_requester_cancels_and_the_block_lifts_at_once(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    cancelled = await client.post(_cancel_url(move_id), headers=headers(scene.admin))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    assert cancelled.json()["uploads"] == []
    row = await _move(db, move_id)
    assert row.status == "cancelled" and row.finished_at is not None
    assert await _group_of(db, scene.post) == scene.home.id
    allowed = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert allowed.status_code == 201, allowed.text
    (event,) = await _events(db, AuditEvent.MOVE_CANCELLED)
    assert event.params["cause"] == "requested" and event.user_id == scene.admin.id

    again = await client.post(_cancel_url(move_id), headers=headers(scene.admin))
    assert again.status_code == 409 and again.json()["code"] == "move_not_waiting", again.text
    assert again.json()["move_status"] == "cancelled"


async def test_anyone_who_could_make_the_same_move_cancels(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    colleague = await _mover_at_root(db)
    cancelled = await client.post(_cancel_url(move_id), headers=headers(colleague))
    assert cancelled.status_code == 200, cancelled.text


async def test_a_holder_at_the_node_only_sees_the_move_but_cannot_cancel_it(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.POST_VIEW, P.POST_UPDATE], Node.post(scene.post.id))
    seen = await client.get(_move_url(move_id), headers=headers(local))
    assert seen.status_code == 200 and seen.json()["uploads"], seen.text
    refused = await client.post(_cancel_url(move_id), headers=headers(local))
    assert refused.status_code == 403 and refused.json()["code"] == "permission_required_for_action", refused.text
    assert (await _move(db, move_id)).status == "waiting"


async def test_an_outsider_gets_404_like_an_unknown_id(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    outsider = await make_user(db, perms=[P.POST_VIEW], post_id=(await make_post(db)).id)
    for answer in (await client.get(_move_url(move_id), headers=headers(outsider)),
                   await client.post(_cancel_url(move_id), headers=headers(outsider)),
                   await client.get(_move_url(move_id + 1000), headers=headers(scene.admin)),
                   await client.post(_cancel_url(move_id + 1000), headers=headers(scene.admin))):
        assert answer.status_code == 404 and answer.json()["code"] == "move_not_found", answer.text


async def test_a_requester_who_lost_the_right_reads_the_end_of_the_move(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    mover = await _mover_at_root(db)
    answer = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(mover))
    assert answer.status_code == 202, answer.text
    move_id = answer.json()["id"]
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()

    seen = await client.get(_move_url(move_id), headers=headers(mover))
    assert seen.status_code == 200 and seen.json()["uploads"] == [], "no upload details without the right"
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(status="completed"))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    seen = await client.get(_move_url(move_id), headers=headers(mover))
    assert seen.json()["status"] == "failed" and seen.json()["error"] == "permission_required_for_action", seen.text


# ---------------------------------------------------------------- checks again when it runs

async def _mover_scene(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    mover = await _mover_at_root(db)
    answer = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(mover))
    assert answer.status_code == 202, answer.text
    return scene, mover, answer.json()["id"]


async def _end_upload(client, headers, scene):
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text


async def test_a_mover_who_lost_the_permission_fails_the_move(client, db, headers, storage):
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()
    await _end_upload(client, headers, scene)

    row = await _move(db, move_id)
    assert (row.status, row.error) == ("failed", "permission_required_for_action")
    assert row.finished_at is not None
    assert await _group_of(db, scene.post) == scene.home.id
    (failed,) = await _events(db, AuditEvent.MOVE_FAILED)
    assert failed.params["failure_code"] == "permission_required_for_action"
    assert failed.user_id == mover.id
    assert await _events(db, AuditEvent.POST_UPDATE) == []


async def test_a_deactivated_mover_fails_the_move(client, db, headers, storage):
    from app.backend.models.user import User

    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    await db.execute(update(User).where(User.id == mover.id).values(is_active=False))
    await db.commit()
    await _end_upload(client, headers, scene)
    assert (await _move(db, move_id)).error == "permission_required_for_action"
    assert await _group_of(db, scene.post) == scene.home.id


async def test_the_destination_may_not_add_grants_the_mover_could_not_give(client, db, headers, storage):
    """H1 at run time: an administrator of the destination appears while the move waits"""
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    administrator = await make_user(db, user_type=UserType.ADMINISTRATOR)
    role = await builtin_role(db, BuiltinRole.ADMINISTRATOR)
    db.add(RoleAssignment(user_id=administrator.id, role_id=role.id, **scope_columns(Node.group(scene.there.id))))
    await db.commit()
    await _end_upload(client, headers, scene)

    row = await _move(db, move_id)
    assert (row.status, row.error) == ("failed", "grant_exceeds_own_permissions")
    assert await _group_of(db, scene.post) == scene.home.id


async def test_an_archived_target_fails_the_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    archived = await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=scene.there.id),
                                 headers=headers(scene.admin))
    assert archived.status_code == 200, archived.text
    await _end_upload(client, headers, scene)
    row = await _move(db, move_id)
    assert (row.status, row.error) == ("failed", "scope_group_not_found")
    assert await _group_of(db, scene.post) == scene.home.id


async def test_a_name_taken_at_the_target_fails_a_group_move(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db, name="Same name"), await make_group(db)
    post = await make_post(db, group_id=home.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=home.id), json={"parent_id": there.id},
                                headers=headers(admin))
    assert answer.status_code == 202, answer.text
    await make_group(db, name="Same name", parent_id=there.id)
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(admin))
    row = await _move(db, answer.json()["id"])
    assert (row.status, row.error) == ("failed", "scope_group_name_taken")
    assert await _parent_of(db, home) is None


async def test_a_cycle_made_while_waiting_fails_the_group_move(client, db, headers, storage):
    admin = await make_admin(db)
    first, second = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=first.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=first.id), json={"parent_id": second.id},
                                headers=headers(admin))
    assert answer.status_code == 202, answer.text
    # the destination moves below the waiting node: no upload there, and its subtree does not hold the waiting node
    moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=second.id), json={"parent_id": first.id},
                               headers=headers(admin))
    assert moved.status_code == 200, moved.text
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(admin))
    row = await _move(db, answer.json()["id"])
    assert (row.status, row.error) == ("failed", "scope_group_cycle")


async def test_the_move_goes_from_where_the_node_sits_when_it_runs(client, db, headers, storage):
    """A post detached by the archive of its brigade while it waited still goes to its target"""
    admin = await make_admin(db)
    corps, there = await make_group(db, kind="corps"), await make_group(db)
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.put(_post_url(post), json={"group_id": there.id}, headers=headers(admin))
    assert answer.status_code == 202, answer.text
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert await _group_of(db, post) == corps.id
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(admin))
    assert (await _move(db, answer.json()["id"])).status == "done"
    assert await _group_of(db, post) == there.id


# ---------------------------------------------------------------- archives

async def test_an_archive_of_the_post_cancels_its_waiting_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=scene.post.id), headers=headers(scene.admin))
    assert archived.status_code == 200, archived.text
    row = await _move(db, move_id)
    assert row.status == "cancelled"
    (event,) = await _events(db, AuditEvent.MOVE_CANCELLED)
    assert event.params["cause"] == "node_archived" and event.user_id == scene.admin.id
    assert await _group_of(db, scene.post) == scene.home.id


async def test_an_archive_of_a_group_cancels_its_waiting_move(client, db, headers, storage):
    """A group is archived only when empty: what can still hold a move then is a completion running at an archived
    post (an archive leaves it to finish)"""
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=home.id), json={"parent_id": there.id},
                                headers=headers(admin))
    assert answer.status_code == 202, answer.text
    await db.execute(update(UploadSession).where(UploadSession.id == upload_id).values(
        status="completing", writer_token="t" * 32, writer_since=datetime.now(timezone.utc)))
    await db.commit()
    assert (await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(admin))).status_code == 200
    assert (await _move(db, answer.json()["id"])).status == "waiting"

    archived = await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=home.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert (await _move(db, answer.json()["id"])).status == "cancelled"
    (event,) = await _events(db, AuditEvent.MOVE_CANCELLED)
    assert event.params["cause"] == "node_archived"


@pytest.mark.parametrize("cascade", [True, False])
async def test_an_archive_of_a_unit_cancels_the_waiting_move_of_the_unit(client, db, headers, storage, cascade):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                json={"corps_id": other.id}, headers=headers(admin))
    assert answer.status_code == 202, answer.text
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                                 params={"cascade": cascade}, headers=headers(admin))
    assert archived.status_code == 200, archived.text
    row = await _move(db, answer.json()["id"])
    assert row.status == "cancelled" and (await _events(db, AuditEvent.MOVE_CANCELLED))[0].params["cause"] == "node_archived"
    assert await _parent_of(db, brigade) == corps.id


async def test_a_cascade_archive_cancels_the_waiting_move_of_a_post_inside(client, db, headers, storage):
    admin = await make_admin(db)
    corps, there = await make_group(db, kind="corps"), await make_group(db)
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.put(_post_url(post), json={"group_id": there.id}, headers=headers(admin))
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                                 params={"cascade": True}, headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert (await _move(db, answer.json()["id"])).status == "cancelled"
    assert await _group_of(db, post) == brigade.id


# ---------------------------------------------------------------- documented

def test_the_contract_declares_the_answers_of_a_move():
    document = V1.openapi(__import__("app.main", fromlist=["fastapi_app"]).fastapi_app)
    operation = document["paths"][V1.path(PostRoutes.POST_BY_ID)]["put"]
    assert "202" in operation["responses"]
    assert {"Location", "Retry-After"} <= set(operation["responses"]["202"]["headers"])
    assert "force" in document["components"]["schemas"]["PostUpdate"]["properties"]
