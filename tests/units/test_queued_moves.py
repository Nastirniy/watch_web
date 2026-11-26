"""Queued moves (spec 2026-10-30): a move with uploads running below the node answers 202 and waits; new uploads below
it answer 409 move_pending, other writes go on; an upload silent for moves.upload_stale_seconds stops holding it and
goes on after the move; force runs it at once; the requester, or anyone who could make the same move, cancels it; the permission
checks run again when it runs, and a mover who lost the right ends it failed with nothing moved.

The SQLite tests cannot race: the concurrent cases were run against MariaDB 11.8 with the real routes."""
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
from tests.media.test_media_uploads import CHUNK, _create_body, make_png, storage  # noqa: F401
from tests.media.test_upload_archive_cancel import CREATE, _observation, _open_session, _part, _stored

pytestmark = pytest.mark.raw_moves      # every request here is sent as written (tests/conftest.py _FormClient)

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


def _move_body(scene, **extra) -> dict:
    """A post move as a form sends it: the target and the place it saw"""
    return {"group_id": scene.there.id, "expected_group_id": scene.home.id, **extra}


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
    answer = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text
    return answer.json()["id"]


# ---------------------------------------------------------------- no upload below the node: as before

async def test_a_move_without_uploads_runs_at_once_and_leaves_no_row(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    moved = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": home.id}, headers=headers(admin))
    assert moved.status_code == 200 and moved.json()["id"] == post.id, moved.text
    assert await _group_of(db, post) == there.id
    assert (await db.exec(select(PendingMove))).all() == []


async def test_a_finished_or_aborted_upload_holds_nothing(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    moved = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(scene.admin))
    assert moved.status_code == 200, moved.text


# ---------------------------------------------------------------- 202 and the operation

async def test_a_post_move_with_an_upload_waits(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    answer = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(scene.admin))
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

    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id), json={"parent_id": there.id, "expected_parent_id": None},
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
                                json={"corps_id": other.id, "expected_corps_id": corps.id}, headers=headers(admin))
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

async def test_a_quiet_upload_stops_holding_the_move_and_goes_on_after_it(client, db, headers, storage, session_maker):
    """Owner, fix round: a quiet upload is left alone. The move runs, the session stays open at the post's new place,
    its client continues and completes"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "done"

    row = await _stored(db, scene.upload_id)
    assert (row.status, row.error) == ("open", None)
    assert _part(storage, scene.upload_id).exists(), "its bytes stay"
    assert await _group_of(db, scene.post) == scene.there.id
    assert await _events(db, AuditEvent.UPLOAD_SESSION_ABORT) == []

    await _send_rest(client, headers, scene.admin, scene.upload_id, 10)
    completed = await client.post(_complete_url(scene.upload_id), headers=headers(scene.admin))
    assert completed.status_code == 200, completed.text


async def test_a_move_with_only_quiet_uploads_below_it_runs_at_once(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _age(db, scene.upload_id)
    moved = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(scene.admin))
    assert moved.status_code == 200, moved.text
    assert await _group_of(db, scene.post) == scene.there.id
    assert (await _stored(db, scene.upload_id)).status == "open"
    assert (await db.exec(select(PendingMove))).all() == [], "no operation: nothing held it"


async def test_a_quiet_upload_that_wakes_up_holds_the_move_again(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    assert (await _chunk(client, headers, scene.admin, scene.upload_id, 10, 10)).status_code == 204
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "waiting"


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
    assert (await _move(db, move_id)).status == "done"
    assert (await _stored(db, scene.upload_id)).status == "open"


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


async def test_a_completion_gone_silent_stops_holding_the_move(client, db, headers, storage, session_maker):
    """An API restart during a complete must not hold a move for COMPLETION_STALE_SECONDS: past
    moves.upload_stale_seconds the claim holds nothing; the session itself is left as it is"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    moment = datetime.now(timezone.utc) - timedelta(seconds=settings.moves.upload_stale_seconds + 60)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(
        status="completing", writer_token="t" * 32, writer_since=moment, last_chunk_at=moment))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "done"
    assert (await _stored(db, scene.upload_id)).status == "completing"


async def test_an_expired_session_no_longer_holds_the_move(client, db, headers, storage, session_maker):
    """Its end is the sweep's (uploads.expire_sessions): a look at a move touches no upload"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    moment = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.execute(update(UploadSession).where(UploadSession.id == scene.upload_id).values(expires_at=moment))
    await db.commit()
    await _worker(session_maker, storage).check_moves()
    assert (await _stored(db, scene.upload_id)).status == "open"
    assert (await _move(db, move_id)).status == "done"


async def test_a_refused_move_touches_no_upload(client, db, headers, storage, session_maker):
    """D4: the checks of the run come first, a look never changes a session"""
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()
    await _age(db, scene.upload_id)
    await _worker(session_maker, storage).check_moves()
    row = await _move(db, move_id)
    assert (row.status, row.error) == ("failed", "permission_required_for_action")
    assert (await _stored(db, scene.upload_id)).status == "open"
    assert _part(storage, scene.upload_id).exists()


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


# ---------------------------------------------------------------- timeouts without the worker

async def test_a_new_upload_that_meets_a_due_move_runs_it_and_goes_on(client, db, headers, storage):
    """Nothing holds the move any more and no worker runs: the refused upload does the work of the worker"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    created = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert created.status_code == 201, created.text
    assert (await _move(db, move_id)).status == "done"
    assert await _group_of(db, scene.post) == scene.there.id


@pytest.mark.parametrize("route", ["multipart", "nginx"])
async def test_a_one_shot_upload_that_meets_a_due_move_runs_it(client, db, headers, storage, route, monkeypatch, tmp_path):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    if route == "multipart":
        stored = await client.post(V1.path(MediaRoutes.UPLOAD), headers=headers(scene.admin),
                                   data={"observation_id": scene.observation.id, "media_type": "image"},
                                   files={"file": ("a.png", DATA, "image/png")})
    else:
        monkeypatch.setattr(settings.storage, "enable_nginx", True)
        monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(tmp_path))
        body = tmp_path / "0000000001"
        body.write_bytes(DATA)
        stored = await client.post(
            V1.path(MediaRoutes.NGINX_UPLOAD), params={"observation_id": scene.observation.id, "media_type": "image"},
            headers={**headers(scene.admin), "X-File-Name": str(body), "X-Original-Filename": "a.png",
                     "Content-Type": "image/png"})
    assert stored.status_code == 200, stored.text
    assert (await _move(db, move_id)).status == "done"


async def test_an_upload_that_ran_a_due_move_answers_for_the_post_s_new_place(client, db, headers, storage):
    """The rights of the request were read before the move: they are asked again at the new place"""
    scene = await _scene(client, db, headers)
    uploader = await make_user(db, perms=[], user_type=UserType.OPERATOR)
    await give(db, uploader, [P.MEDIA_VIEW, P.MEDIA_CREATE], Node.group(scene.home.id))
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    refused = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(uploader))
    assert refused.status_code == 403, refused.text
    assert (await _move(db, move_id)).status == "done"


async def test_an_upload_refused_by_a_move_that_is_held_runs_nothing(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    refused = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text
    assert (await _move(db, move_id)).status == "waiting"


async def test_a_due_move_refused_at_its_run_lets_the_upload_through_at_the_old_place(client, db, headers, storage):
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()
    await _age(db, scene.upload_id)
    created = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert created.status_code == 201, created.text
    assert (await _move(db, move_id)).error == "permission_required_for_action"
    assert await _group_of(db, scene.post) == scene.home.id


async def test_reading_a_move_runs_it_when_it_is_due(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    seen = await client.get(_move_url(move_id), headers=headers(scene.admin))
    assert seen.json()["status"] == "waiting" and seen.json()["uploads"][0]["holding"] is True
    await _age(db, scene.upload_id)
    seen = await client.get(_move_url(move_id), headers=headers(scene.admin))
    assert seen.status_code == 200 and seen.json()["status"] == "done", seen.text
    assert seen.json()["uploads"] == [] and seen.json()["finished_at"]
    assert await _group_of(db, scene.post) == scene.there.id
    (done,) = await _events(db, AuditEvent.MOVE_COMPLETED)
    assert done.user_id == scene.admin.id, "the run is the requester's, whoever looked"


async def test_a_look_of_a_request_and_of_the_worker_run_the_move_once(client, db, headers, storage, session_maker):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    await client.get(_move_url(move_id), headers=headers(scene.admin))
    await _worker(session_maker, storage).check_moves()
    assert len(await _events(db, AuditEvent.MOVE_COMPLETED)) == 1


async def test_a_move_request_that_meets_a_due_move_runs_it_and_is_told_to_try_again(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)
    third = await make_group(db)
    refused = await client.put(_post_url(scene.post), json={"group_id": third.id, "expected_group_id": scene.home.id},
                               headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["code"] == "move_pending", refused.text
    assert (await _move(db, move_id)).status == "done"


async def test_a_refused_file_at_complete_ends_the_last_upload_and_runs_the_move(client, db, headers, storage):
    """D7: a complete that ends the session runs the move whatever became of the file"""
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    observation = await _observation(db, admin, post)
    created = await client.post(CREATE, json=_create_body(observation.id, DATA, sha256="0" * 64), headers=headers(admin))
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    await _send_rest(client, headers, admin, upload_id, 0)
    moved = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": home.id}, headers=headers(admin))
    assert moved.status_code == 202, moved.text
    refused = await client.post(_complete_url(upload_id), headers=headers(admin))
    assert refused.status_code == 422 and refused.json()["code"] == "upload_checksum_mismatch", refused.text
    assert (await _move(db, moved.json()["id"])).status == "done"
    assert await _group_of(db, post) == there.id


async def test_a_failed_run_does_not_break_the_answer_of_the_upload_that_caused_it(client, db, headers, storage):
    """The rollback of a refused run expires what the session holds: another waiting move must not be read from it
    (a lazy load in an async session: 500 StatementError, greenlet_spawn; seen once in the review's chaos run)"""
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    other_upload = await _open_session(client, headers, scene.admin,
                                       await _observation(db, scene.admin, await make_post(db)), DATA, 10)
    elsewhere = (await _stored(db, other_upload)).post_id
    target = await make_group(db)
    second = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=elsewhere),
                              json={"group_id": target.id, "expected_group_id": None}, headers=headers(scene.admin))
    assert second.status_code == 202 and second.json()["id"] > move_id, second.text
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()

    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    assert (await _move(db, move_id)).status == "failed"
    assert (await _move(db, second.json()["id"])).status == "waiting"


async def test_no_second_session_when_no_move_waits(client, db, headers, storage, monkeypatch):
    """D3: the pool is a known risk; the end of an upload reads the waiting moves on its own session"""
    from app.backend.api.shared.helpers import move_helper

    opened = []
    real = move_helper.AsyncSession
    monkeypatch.setattr(move_helper, "AsyncSession", lambda *args, **kw: opened.append(1) or real(*args, **kw))
    scene = await _scene(client, db, headers)
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    assert opened == []


@pytest.mark.parametrize("failure", ["deadlock", "pool"])
async def test_a_database_that_refuses_the_look_leaves_the_upload_s_answer_alone(
        client, db, headers, storage, monkeypatch, session_maker, failure):
    """The upload has ended: its answer does not depend on the move that waited for it. D3: a pool that has no
    connection left is sqlalchemy.exc.TimeoutError, which is no OperationalError"""
    from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)

    async def refused(*args):
        raise PoolTimeout("QueuePool limit") if failure == "pool" else OperationalError("x", {}, Exception(1213, "deadlock"))

    with monkeypatch.context() as patch:
        patch.setattr(moves_service, "advance", refused)
        aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                    headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    assert (await _move(db, move_id)).status == "waiting"
    await _worker(session_maker, storage).check_moves()
    assert (await _move(db, move_id)).status == "done"


async def test_a_database_that_refuses_the_look_answers_the_refused_upload_as_refused(
        client, db, headers, storage, monkeypatch):
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    await _age(db, scene.upload_id)

    async def refused(*args):
        raise PoolTimeout("QueuePool limit")

    monkeypatch.setattr(moves_service, "run_move", refused)
    answer = await client.post(CREATE, json=_create_body(scene.observation.id, DATA), headers=headers(scene.admin))
    assert answer.status_code == 409 and answer.json()["move_id"] == move_id, answer.text
    seen = await client.get(_move_url(move_id), headers=headers(scene.admin))
    assert seen.status_code == 200 and seen.json()["status"] == "waiting", seen.text


async def test_health_counts_the_waiting_moves(client, db, headers, storage, monkeypatch):
    from app.backend.enums.routes import SystemRoutes

    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    idle = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert idle["moves"] == {"waiting": 0, "oldest_waiting_seconds": None}
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    moment = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db.execute(update(PendingMove).where(PendingMove.id == move_id).values(requested_at=moment))
    await db.commit()
    part = (await client.get(SystemRoutes.HEALTH.value)).json()["moves"]
    assert part["waiting"] == 1 and 600 <= part["oldest_waiting_seconds"] < 700


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
        moved = await client.put(_post_url(post), json={"group_id": other.id, "expected_group_id": brigade.id, "force": True}, headers=headers(admin))
        assert await _group_of(db, post) == other.id
    elif kind == "group":
        target = await make_group(db)
        plain = await make_group(db)
        await db.execute(update(Post).where(Post.id == post.id).values(group_id=plain.id))
        await db.commit()
        moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=plain.id),
                                   json={"parent_id": target.id, "expected_parent_id": None, "force": True}, headers=headers(admin))
        assert await _parent_of(db, plain) == target.id
    else:
        moved = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                   json={"corps_id": other.id, "expected_corps_id": corps.id, "force": True}, headers=headers(admin))
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
    moved = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": home.id, "force": True}, headers=headers(admin))
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

    moved = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": home.id, "force": True}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    refused = await client.post(_complete_url(upload_id), headers=headers(uploader))
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------- a move is its own request

async def test_a_waiting_move_refuses_other_fields(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    refused = await client.put(_post_url(scene.post), json=_move_body(scene, name="New name"),
                               headers=headers(scene.admin))
    assert refused.status_code == 422 and refused.json()["code"] == "move_not_alone", refused.text
    assert refused.json()["changed_fields"] == ["name"]
    assert (await db.exec(select(PendingMove))).all() == []


async def test_fields_sent_with_their_current_value_do_not_count(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    answer = await client.put(_post_url(scene.post), json=_move_body(scene, name=scene.post.name),
                              headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text


@pytest.mark.parametrize("body", [{"force": True}, {}])
async def test_a_move_with_other_fields_runs_as_before_when_it_need_not_wait(client, db, headers, storage, body):
    scene = await _scene(client, db, headers, sent=0)
    if not body:
        await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id), headers=headers(scene.admin))
    moved = await client.put(_post_url(scene.post), json=_move_body(scene, name="Moved", **body),
                             headers=headers(scene.admin))
    assert moved.status_code == 200 and moved.json()["name"] == "Moved", moved.text


async def test_a_group_rename_with_a_move_that_would_wait_is_refused(client, db, headers, storage):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    refused = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                                 json={"corps_id": other.id, "expected_corps_id": corps.id, "name": "Other name"}, headers=headers(admin))
    assert refused.status_code == 422 and refused.json()["changed_fields"] == ["name"], refused.text


# ---------------------------------------------------------------- a move states where it thinks the node is

class _Node:
    """A node of one of the three move routes in the group home, and a group there to go to"""

    def __init__(self, kind, admin, row, home, there):
        self.kind, self.admin, self.row, self.home, self.there = kind, admin, row, home, there
        self.url = {"post": lambda: _post_url(row),
                    "group": lambda: V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=row.id),
                    "brigade": lambda: V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=row.id)}[kind]()
        self.target, self.expected = {"post": ("group_id", "expected_group_id"),
                                      "group": ("parent_id", "expected_parent_id"),
                                      "brigade": ("corps_id", "expected_corps_id")}[kind]

    def body(self, target, **extra):
        return {self.target: target, **extra}

    def moving(self, target, expected, **extra):
        return {self.target: target, self.expected: expected, **extra}

    async def send(self, client, headers, body, user=None):
        method = client.put if self.kind == "post" else client.patch
        return await method(self.url, json=body, headers=headers(user or self.admin))

    async def place(self, db):
        return await (_group_of if self.kind == "post" else _parent_of)(db, self.row)


async def _node(db, kind) -> _Node:
    admin = await make_admin(db)
    if kind == "post":
        home, there = await make_group(db), await make_group(db)
        return _Node(kind, admin, await make_post(db, group_id=home.id), home, there)
    if kind == "group":
        home, there = await make_group(db), await make_group(db)
        return _Node(kind, admin, await make_group(db, parent_id=home.id), home, there)
    home, there = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    return _Node(kind, admin, await make_group(db, parent_id=home.id, kind="brigade"), home, there)


KINDS = ["post", "group", "brigade"]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_move_with_the_expected_place_runs(client, db, headers, storage, kind):
    node = await _node(db, kind)
    moved = await node.send(client, headers, node.moving(node.there.id, node.home.id))
    assert moved.status_code == 200 and moved.json()["already_there"] is False, moved.text
    assert await node.place(db) == node.there.id


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("extra", [{}, {"force": True}])
async def test_a_move_without_an_expected_place_is_a_conflict(client, db, headers, storage, kind, extra):
    node = await _node(db, kind)
    refused = await node.send(client, headers, node.body(node.there.id, **extra))
    assert refused.status_code == 409 and refused.json()["code"] == "move_conflict", refused.text
    assert refused.json()["current_place"] == "group" and refused.json()["current_group_id"] == node.home.id
    assert await node.place(db) == node.home.id


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("extra", [{}, {"force": True}])
async def test_a_move_with_another_expected_place_is_a_conflict(client, db, headers, storage, kind, extra):
    node = await _node(db, kind)
    other = await make_group(db, kind="corps" if kind == "brigade" else "group")
    refused = await node.send(client, headers, node.moving(node.there.id, other.id, **extra))
    assert refused.status_code == 409 and refused.json()["code"] == "move_conflict", refused.text
    assert refused.json()["current_group_id"] == node.home.id
    assert await node.place(db) == node.home.id


@pytest.mark.parametrize("kind", KINDS)
async def test_the_node_already_at_the_target_answers_200_and_writes_nothing(client, db, headers, storage, kind):
    node = await _node(db, kind)
    assert (await node.send(client, headers, node.moving(node.there.id, node.home.id))).status_code == 200
    before = await db.exec(select(AuditLog))
    written = len(before.all())
    stamp = (await client.get(node.url, headers=headers(node.admin))).json()["updated_at"]

    # the same form from a second administrator, with a change of its own
    again = await node.send(client, headers, node.moving(node.there.id, node.home.id, name="Not written", force=True))
    assert again.status_code == 200, again.text
    assert again.json()["already_there"] is True and again.json()["name"] == node.row.name
    seen = (await client.get(node.url, headers=headers(node.admin))).json()
    assert seen["updated_at"] == stamp and seen["name"] == node.row.name
    assert len((await db.exec(select(AuditLog))).all()) == written, "no audit row"


@pytest.mark.parametrize("kind", KINDS)
async def test_the_current_value_without_an_expected_place_is_an_ordinary_edit(client, db, headers, storage, kind):
    node = await _node(db, kind)
    edited = await node.send(client, headers, node.body(node.home.id, name="Edited"))
    assert edited.status_code == 200 and edited.json()["name"] == "Edited", edited.text
    assert edited.json()["already_there"] is False


@pytest.mark.parametrize("kind", KINDS)
async def test_a_form_that_leaves_the_place_as_it_saw_it_is_an_ordinary_edit(client, db, headers, storage, kind):
    """Target, expected place and the row all agree: nothing asks for a move, and the rename of the form must not be
    lost to already_there"""
    node = await _node(db, kind)
    edited = await node.send(client, headers, node.moving(node.home.id, node.home.id, name="Edited"))
    assert edited.status_code == 200 and edited.json()["name"] == "Edited", edited.text
    assert edited.json()["already_there"] is False


@pytest.mark.parametrize("kind", KINDS)
async def test_a_stale_form_answers_409_and_moves_nothing(client, db, headers, storage, kind):
    """The node moved after the form was read; the form sends its old value, as a client that sends the whole record"""
    node = await _node(db, kind)
    assert (await node.send(client, headers, node.moving(node.there.id, node.home.id))).status_code == 200
    stale = await node.send(client, headers, node.body(node.home.id, name="From the old form"))
    assert stale.status_code == 409 and stale.json()["code"] == "move_conflict", stale.text
    assert stale.json()["current_group_id"] == node.there.id
    assert await node.place(db) == node.there.id
    assert (await client.get(node.url, headers=headers(node.admin))).json()["name"] == node.row.name


async def test_presence_counts_null_is_a_place(client, db, headers, storage):
    """A top-level node: null is the value it holds, an absent expected value is another thing"""
    admin = await make_admin(db)
    group, there = await make_group(db), await make_group(db)
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=group.id)
    missing = await client.patch(url, json={"parent_id": there.id}, headers=headers(admin))
    assert missing.status_code == 409 and missing.json()["current_place"] == "root", missing.text
    assert "current_group_id" not in missing.json()
    moved = await client.patch(url, json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    back = await client.patch(url, json={"parent_id": None, "expected_parent_id": there.id}, headers=headers(admin))
    assert back.status_code == 200 and back.json()["parent_id"] is None, back.text
    done = await client.patch(url, json={"parent_id": None, "expected_parent_id": there.id}, headers=headers(admin))
    assert done.status_code == 200 and done.json()["already_there"] is True, done.text


async def test_a_post_at_the_root_states_null(client, db, headers, storage):
    admin = await make_admin(db)
    post, there = await make_post(db), await make_group(db)
    moved = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": None}, headers=headers(admin))
    assert moved.status_code == 200, moved.text


async def test_the_conflict_does_not_name_a_group_the_caller_cannot_see(client, db, headers, storage):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.POST_VIEW, P.POST_UPDATE], Node.post(post.id))
    refused = await client.put(_post_url(post), json={"group_id": there.id}, headers=headers(local))
    assert refused.status_code == 409 and refused.json()["code"] == "move_conflict", refused.text
    assert refused.json()["current_place"] == "hidden" and "current_group_id" not in refused.json()
    assert (await client.put(_post_url(post), json={"group_id": there.id}, headers=headers(admin))).status_code == 409


async def test_a_conflict_on_a_group_needs_the_right_to_edit_it(client, db, headers, storage):
    """Scope before state: a holder at the group who could not edit it learns nothing from a 409"""
    node = await _node(db, "group")
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_UPDATE], Node.group(node.row.id))
    refused = await node.send(client, headers, node.body(node.there.id), user=local)
    assert refused.status_code == 403 and refused.json()["code"] == "permission_required_for_action", refused.text


async def test_a_request_with_the_same_value_takes_no_move_lock(client, db, headers, storage, monkeypatch):
    """Item 7: only a value that really differs takes the global lock, whatever the kind of request"""
    calls = []
    real = moves_service.lock_for_move

    async def counting(*args):
        calls.append(args[2])
        return await real(*args)

    monkeypatch.setattr(moves_service, "lock_for_move", counting)
    for kind in KINDS:
        node = await _node(db, kind)
        assert (await node.send(client, headers, node.body(node.home.id, name="Same place"))).status_code == 200
        assert (await node.send(client, headers, node.moving(node.home.id, node.home.id))).status_code == 200
    assert calls == []
    node = await _node(db, "post")
    assert (await node.send(client, headers, node.moving(node.there.id, node.home.id))).status_code == 200
    assert len(calls) == 1
    # the node already at the target: nothing to serialise
    assert (await node.send(client, headers, node.moving(node.there.id, node.home.id))).json()["already_there"] is True
    assert len(calls) == 1


async def test_a_target_inside_the_node_is_refused_without_the_locks_of_a_move(client, db, headers, storage, monkeypatch):
    calls = []
    real = moves_service.lock_for_move

    async def counting(*args):
        calls.append(args[2])
        return await real(*args)

    monkeypatch.setattr(moves_service, "lock_for_move", counting)
    node = await _node(db, "group")
    inside = await make_group(db, parent_id=node.row.id)
    refused = await node.send(client, headers, node.moving(inside.id, node.home.id))
    assert refused.status_code == 409 and refused.json()["code"] == "scope_group_cycle", refused.text
    assert calls == []


async def test_a_move_the_tree_did_not_see_coming_is_a_conflict(client, db, headers, storage, monkeypatch):
    """The tree of the request said no move, the locked row says one: it moved in between. The locks of a move come
    first or not at all, and the answer is the one of a stale form"""
    node = await _node(db, "post")
    monkeypatch.setattr(moves_service, "takes_move_locks", lambda *args: False)
    refused = await node.send(client, headers, node.moving(node.there.id, node.home.id))
    assert refused.status_code == 409 and refused.json()["code"] == "move_conflict", refused.text
    assert await node.place(db) == node.home.id


# ---------------------------------------------------------------- a repeat of a waiting move

async def test_a_repeat_of_a_waiting_move_answers_that_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    colleague = await make_admin(db)
    for user in (scene.admin, colleague):
        again = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(user))
        assert again.status_code == 202 and again.json()["id"] == move_id, again.text
        assert again.json()["status"] == "waiting" and again.json()["uploads"]
        assert again.headers["Location"] == _move_url(move_id) and "Retry-After" in again.headers
    assert len((await db.exec(select(PendingMove))).all()) == 1
    assert len(await _events(db, AuditEvent.MOVE_REQUESTED)) == 1, "one operation, one audit row"
    assert (await _move(db, move_id)).requested_by == scene.admin.id, "it stays the first requester's"


async def test_a_repeat_needs_the_rights_of_the_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.POST_VIEW, P.POST_UPDATE], Node.post(scene.post.id))
    refused = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(local))
    assert refused.status_code == 403 and refused.json()["code"] == "permission_required_for_action", refused.text


async def test_a_repeat_states_the_place_like_any_move(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    refused = await client.put(_post_url(scene.post), json={"group_id": scene.there.id}, headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["code"] == "move_conflict", refused.text


async def test_a_repeat_with_other_fields_is_refused_like_a_move_that_waits(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    refused = await client.put(_post_url(scene.post), json=_move_body(scene, name="Renamed"), headers=headers(scene.admin))
    assert refused.status_code == 422 and refused.json()["code"] == "move_not_alone", refused.text


async def test_a_forced_repeat_is_no_repeat(client, db, headers, storage):
    """force asks for something the waiting move is not: it is refused, not dropped in silence (cancel, then force)"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    refused = await client.put(_post_url(scene.post), json=_move_body(scene, force=True), headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text
    assert await _group_of(db, scene.post) == scene.home.id


# ---------------------------------------------------------------- a node on the way is busy

class _Busy:
    """A group that waits to go to a target. node: a group inside, with the post whose upload holds the move; target
    sits inside holder; free and free_post are elsewhere"""


async def _busy(client, db, headers) -> _Busy:
    scene = _Busy()
    scene.admin = await make_admin(db)
    scene.waiting = await make_group(db)
    scene.inner = await make_group(db, parent_id=scene.waiting.id)
    scene.post = await make_post(db, group_id=scene.inner.id)
    scene.holder = await make_group(db)
    scene.target = await make_group(db, parent_id=scene.holder.id)
    scene.free, scene.other = await make_group(db), await make_group(db)
    scene.free_post = await make_post(db, group_id=scene.free.id)
    scene.upload_id = await _open_session(client, headers, scene.admin, await _observation(db, scene.admin, scene.post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=scene.waiting.id),
                                json={"parent_id": scene.target.id, "expected_parent_id": None}, headers=headers(scene.admin))
    assert answer.status_code == 202, answer.text
    scene.move_id = answer.json()["id"]
    return scene


def _group_move(group, target, expected, **extra):
    return V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=group.id), {
        "parent_id": target.id if target else None, "expected_parent_id": expected, **extra}


BUSY_CASES = {
    "the node, to another target": lambda s: _group_move(s.waiting, s.other, None),
    "a group below the node": lambda s: _group_move(s.inner, s.other, s.waiting.id),
    "a post below the node": lambda s: (_post_url(s.post), {"group_id": s.other.id, "expected_group_id": s.inner.id}),
    "a group into the node": lambda s: _group_move(s.free, s.waiting, None),
    "a group into a group below the node": lambda s: _group_move(s.free, s.inner, None),
    "a post into the node": lambda s: (_post_url(s.free_post), {"group_id": s.waiting.id, "expected_group_id": s.free.id}),
    "the target": lambda s: _group_move(s.target, s.other, s.holder.id),
    "the target below the waiting node": lambda s: _group_move(s.target, s.inner, s.holder.id),
    "a group holding the target": lambda s: _group_move(s.holder, s.other, None),
}


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("case", list(BUSY_CASES))
async def test_a_node_on_the_way_is_busy(client, db, headers, storage, case, force):
    scene = await _busy(client, db, headers)
    url, body = BUSY_CASES[case](scene)
    method = client.put if "group_id" in body else client.patch
    refused = await method(url, json={**body, **({"force": True} if force else {})}, headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["code"] == "move_pending", refused.text
    assert refused.json()["move_id"] == scene.move_id
    assert refused.headers["Retry-After"] == str(settings.moves.check_interval_seconds)


async def test_a_move_past_the_busy_nodes_goes_on(client, db, headers, storage):
    scene = await _busy(client, db, headers)
    url, body = _group_move(scene.free, scene.target, None)
    moved = await client.patch(url, json=body, headers=headers(scene.admin))
    assert moved.status_code == 200, "a second node may go to the same target"


async def test_two_moves_going_below_each_other_cannot_wait_together(client, db, headers, storage):
    """The review's late cycle failure: the second move is refused at once"""
    admin = await make_admin(db)
    first, second = await make_group(db), await make_group(db)
    await _open_session(client, headers, admin, await _observation(db, admin, await make_post(db, group_id=first.id)), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=first.id),
                                json={"parent_id": second.id, "expected_parent_id": None}, headers=headers(admin))
    assert answer.status_code == 202, answer.text
    refused = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=second.id),
                                 json={"parent_id": first.id, "expected_parent_id": None}, headers=headers(admin))
    assert refused.status_code == 409 and refused.json()["move_id"] == answer.json()["id"], refused.text


async def test_a_node_may_move_again_when_the_move_has_ended(client, db, headers, storage):
    scene = await _busy(client, db, headers)
    await client.post(_cancel_url(scene.move_id), headers=headers(scene.admin))
    url, body = _group_move(scene.target, scene.other, scene.holder.id)
    moved = await client.patch(url, json=body, headers=headers(scene.admin))
    assert moved.status_code == 200, moved.text


# ---------------------------------------------------------------- overlapping moves

async def test_moves_overlapping_a_waiting_one_answer_move_pending(client, db, headers, storage):
    admin = await make_admin(db)
    top, there = await make_group(db), await make_group(db)
    post, beside = await make_post(db, group_id=top.id), await make_post(db, group_id=top.id)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id),
                                json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
    assert answer.status_code == 202, answer.text
    move_id = answer.json()["id"]

    for body, url, method in (({"group_id": there.id, "expected_group_id": top.id}, _post_url(beside), client.put),
                              ({"group_id": there.id, "expected_group_id": top.id, "force": True}, _post_url(post), client.put),
                              ({"parent_id": None}, V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id), client.patch)):
        refused = await method(url, json=body, headers=headers(admin))
        if body == {"parent_id": None}:
            assert refused.status_code == 200, "its own parent sent back is no move"
            continue
        assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text
        assert refused.headers["Retry-After"] == str(settings.moves.check_interval_seconds)

    second_there = await make_group(db)
    again = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id),
                               json={"parent_id": second_there.id, "expected_parent_id": None}, headers=headers(admin))
    assert again.status_code == 409 and again.json()["move_id"] == move_id, again.text


async def test_a_move_of_a_group_above_a_waiting_post_move_answers_move_pending(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    refused = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=scene.home.id),
                                 json={"parent_id": scene.there.id, "expected_parent_id": None}, headers=headers(scene.admin))
    assert refused.status_code == 409 and refused.json()["move_id"] == move_id, refused.text


async def test_a_move_elsewhere_is_not_blocked_by_a_waiting_one(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    other = await make_post(db, group_id=scene.there.id)
    moved = await client.put(_post_url(other), json={"group_id": scene.home.id, "expected_group_id": scene.there.id}, headers=headers(scene.admin))
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
    answer = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(mover))
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


async def test_a_holder_at_the_post_alone_is_not_told_the_target(client, db, headers, storage):
    """D6: the post is theirs, the unit it goes to may not be: the id stays out, a flag tells it from the root"""
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.POST_VIEW, P.POST_UPDATE], Node.post(scene.post.id))
    seen = await client.get(_move_url(move_id), headers=headers(local))
    assert seen.status_code == 200, seen.text
    assert seen.json()["target_group_id"] is None and seen.json()["target_visible"] is False

    full = await client.get(_move_url(move_id), headers=headers(scene.admin))
    assert full.json()["target_group_id"] == scene.there.id and full.json()["target_visible"] is True


async def test_the_requester_always_sees_the_target_of_their_move(client, db, headers, storage):
    scene, mover, move_id = await _mover_scene(client, db, headers, storage)
    for grant in (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == mover.id))).all():
        await db.delete(grant)
    await db.commit()
    seen = await client.get(_move_url(move_id), headers=headers(mover))
    assert seen.json()["target_group_id"] == scene.there.id and seen.json()["target_visible"] is True


async def test_the_root_is_no_unit_to_hide(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    answer = await client.put(_post_url(scene.post), json={"group_id": None, "expected_group_id": scene.home.id},
                              headers=headers(scene.admin))
    assert answer.status_code == 202 and answer.json()["target_visible"] is True, answer.text
    local = await make_user(db, perms=[], user_type=UserType.ADMINISTRATOR)
    await give(db, local, [P.POST_VIEW, P.POST_UPDATE], Node.post(scene.post.id))
    seen = await client.get(_move_url(answer.json()["id"]), headers=headers(local))
    assert seen.json()["target_group_id"] is None and seen.json()["target_visible"] is True


# ---------------------------------------------------------------- checks again when it runs

async def _mover_scene(client, db, headers, storage):
    scene = await _scene(client, db, headers)
    mover = await _mover_at_root(db)
    answer = await client.put(_post_url(scene.post), json=_move_body(scene), headers=headers(mover))
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
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=home.id),
                                json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
    assert answer.status_code == 202, answer.text
    await make_group(db, name="Same name", parent_id=there.id)
    await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(admin))
    row = await _move(db, answer.json()["id"])
    assert (row.status, row.error) == ("failed", "scope_group_name_taken")
    assert await _parent_of(db, home) is None


async def test_the_move_goes_from_where_the_node_sits_when_it_runs(client, db, headers, storage):
    """A post detached by the archive of its brigade while it waited still goes to its target"""
    admin = await make_admin(db)
    corps, there = await make_group(db, kind="corps"), await make_group(db)
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    upload_id = await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    answer = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": brigade.id}, headers=headers(admin))
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
    answer = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=home.id),
                                json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
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
                                json={"corps_id": other.id, "expected_corps_id": corps.id}, headers=headers(admin))
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
    answer = await client.put(_post_url(post), json={"group_id": there.id, "expected_group_id": brigade.id}, headers=headers(admin))
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                                 params={"cascade": True}, headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert (await _move(db, answer.json()["id"])).status == "cancelled"
    assert await _group_of(db, post) == brigade.id


# ---------------------------------------------------------------- locks

async def test_the_pending_sessions_are_locked_through_the_parent_index_never_status(monkeypatch, db):
    """A locking read over the status index deadlocks with an abort holding the row (MariaDB 1213, found live)"""
    from sqlalchemy.dialects import mysql

    from app.backend.services.media import uploads

    seen = []

    async def recording(session, query, shared=False):
        seen.append(str(query.compile(dialect=mysql.dialect())))
        return []

    monkeypatch.setattr(uploads, "lock_all", recording)
    await uploads.lock_pending(db, post_ids=[1])
    await uploads.lock_pending(db, observation_id=1)
    assert "FORCE INDEX (ix_upload_sessions_post_id)" in seen[0]
    assert "FORCE INDEX (ix_upload_sessions_observation_id)" in seen[1]


class _Locks:
    """Every locking read of the module functions lock_all, as MariaDB gets it, in order"""

    def __init__(self, monkeypatch):
        from sqlalchemy.dialects import mysql

        from app.backend.services import move_queue, scope_groups
        from app.backend.utils import row_locks

        self.seen = []
        real = row_locks.lock_all

        async def recording(session, query, shared=False):
            self.seen.append(str(query.compile(dialect=mysql.dialect())))
            return await real(session, query, shared)

        for module in (moves_service, scope_groups, move_queue, row_locks):
            monkeypatch.setattr(module, "lock_all", recording)


async def test_the_posts_of_a_group_move_are_locked_by_id_first(client, db, headers, storage, monkeypatch):
    """Locks taken through ix_posts_group_id follow (group_id, id), whatever ORDER BY says (the filesort comes after):
    against a scan by the primary key that deadlocks (the review's 1213 on a brigade archive). Point lookups by id come
    first, then the index, for what the tree of the request did not know"""
    admin = await make_admin(db)
    top, inner, there = await make_group(db), await make_group(db), await make_group(db)
    await db.execute(update(ScopeGroup).where(ScopeGroup.id == inner.id).values(parent_id=top.id, parent_key=top.id))
    await db.commit()
    await make_post(db, group_id=inner.id)
    await make_post(db, group_id=top.id)
    locks = _Locks(monkeypatch)
    moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id),
                               json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    posts = [statement for statement in locks.seen if "FROM posts" in statement]
    assert "FORCE INDEX (PRIMARY)" in posts[0] and "posts.id IN" in posts[0], posts
    assert all("FORCE INDEX (ix_posts_group_id)" in statement and "group_id = " in statement for statement in posts[1:])
    assert len(posts) == 3, "one statement per group"


async def test_the_groups_below_a_move_are_locked_through_the_parent_index(client, db, headers, storage, monkeypatch):
    """D2: with a long IN list the query scanned the primary key and locked every group: 1213 on a create and a rename"""
    admin = await make_admin(db)
    top, there = await make_group(db), await make_group(db)
    locks = _Locks(monkeypatch)
    moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=top.id),
                               json={"parent_id": there.id, "expected_parent_id": None}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    below = [statement for statement in locks.seen if "FROM scope_groups" in statement]
    assert below and all("FORCE INDEX (ix_scope_groups_parent_id)" in statement or "FORCE INDEX (PRIMARY)" in statement
                         for statement in below), below


async def test_a_long_list_is_locked_in_short_statements(db, monkeypatch):
    """A long IN list is another query: a scan of the whole index, or a join (MariaDB, in_predicate_conversion_threshold)"""
    from app.backend.utils import row_locks

    seen = []

    async def recording(session, query, shared=False):
        seen.append(len(query.whereclause.right.value))
        return []

    monkeypatch.setattr(row_locks, "lock_all", recording)
    await row_locks.lock_ids(db, Post, range(1, row_locks.LOCK_BATCH * 2 + 2))
    assert seen == [row_locks.LOCK_BATCH, row_locks.LOCK_BATCH, 1]


@pytest.mark.parametrize("cascade", [True, False])
async def test_a_unit_archive_locks_posts_like_a_move(client, db, headers, storage, monkeypatch, cascade):
    """Both orders must be the one order: the query of an archive scanned the primary key where the table allows"""
    admin = await make_admin(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    await make_post(db, group_id=brigade.id)
    locks = _Locks(monkeypatch)
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), params={"cascade": cascade},
                                 headers=headers(admin))
    assert archived.status_code == 200, archived.text
    posts = [statement for statement in locks.seen if "FROM posts" in statement]
    assert "FORCE INDEX (PRIMARY)" in posts[0] and "posts.id IN" in posts[0], posts
    assert all("FORCE INDEX (ix_posts_group_id)" in statement for statement in posts[1:]) and posts[1:], posts
    groups = [statement for statement in locks.seen if "FROM scope_groups" in statement]
    assert all("FORCE INDEX (ix_scope_groups_parent_id)" in statement for statement in groups) and groups, groups


async def test_waiting_moves_are_locked_through_an_index_without_status(client, db, headers, storage, monkeypatch):
    """D5: a cancel's UPDATE by primary key rewrites the status index; a locking read over it deadlocks with that"""
    scene = await _scene(client, db, headers)
    await _waiting_move(client, headers, scene)
    locks = _Locks(monkeypatch)
    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=scene.post.id), headers=headers(scene.admin))
    assert archived.status_code == 200, archived.text
    waiting = [statement for statement in locks.seen if "FROM pending_moves" in statement]
    assert waiting and all("FORCE INDEX (ix_pending_moves_node)" in statement for statement in waiting), waiting


async def test_a_group_create_locks_its_parent_first(client, db, headers, storage, monkeypatch):
    """Without the lock the insert waits for the parent holding its new row: 1213 with a move, 1020 with a rename"""
    from app.backend.utils import row_locks

    admin = await make_admin(db)
    parent = await make_group(db)
    h = headers(admin)
    await db.commit()
    seen = []
    real = row_locks.lock_first

    async def recording(session, query, shared=False):
        seen.append(str(query))
        return await real(session, query, shared)

    from app.backend.services import scope_groups

    monkeypatch.setattr(scope_groups, "lock_first", recording)
    created = await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Below", "parent_id": parent.id}, headers=h)
    assert created.status_code == 200, created.text
    assert len(seen) == 1 and "scope_groups" in seen[0]


# ---------------------------------------------------------------- documented

def test_the_contract_declares_the_answers_of_a_move():
    document = V1.openapi(__import__("app.main", fromlist=["fastapi_app"]).fastapi_app)
    operation = document["paths"][V1.path(PostRoutes.POST_BY_ID)]["put"]
    assert "202" in operation["responses"]
    assert {"Location", "Retry-After"} <= set(operation["responses"]["202"]["headers"])
    assert "force" in document["components"]["schemas"]["PostUpdate"]["properties"]
