"""Resumable uploads: session lifecycle, offsets, ownership, completion, abort and expiry."""
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select
from starlette.requests import ClientDisconnect

from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.backend.errors.coded import CodedError
from app.backend.models.audit import AuditLog
from app.backend.models.media import MediaFile
from app.backend.services.media import uploads
from app.backend.services.media.manager import get_media_manager, reset_media_manager
from tests.helpers import make_admin, make_post, make_user, make_dictionary, make_observation

CREATE = V1.path(MediaRoutes.UPLOADS)
SESSION = V1.path(MediaRoutes.UPLOAD_SESSION)
COMPLETE = V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE)
ABORT = V1.path(MediaRoutes.UPLOAD_SESSION_ABORT)
CHUNK = {"Content-Type": "application/offset+octet-stream"}


def make_png(width=64, height=48) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (width, height), (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()
    yield tmp_path
    reset_media_manager()


async def _scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, user, observation


def _create_body(observation_id: int, data: bytes, **overrides) -> dict:
    body = {"observation_id": observation_id, "media_type": "image", "original_filename": "photo.png",
            "expected_size": len(data), "content_type": "image/png"}
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_full_resumable_flow_with_interruption(client, db, headers, storage):
    post, user, observation = await _scene(db)
    data = make_png(400, 300)
    digest = hashlib.sha256(data).hexdigest()

    created = await client.post(CREATE, json=_create_body(observation.id, data, sha256=digest), headers=headers(user))
    assert created.status_code == 201, created.text
    body = created.json()
    upload_id = body["id"]
    assert body["status"] == "open" and body["received_bytes"] == 0 and body["expected_size"] == len(data)
    assert created.headers["upload-offset"] == "0" and created.headers["upload-length"] == str(len(data))
    assert created.headers["location"].endswith(upload_id)
    assert body["urls"]["complete"].endswith("/complete") and body["max_chunk_bytes"] > 0

    # first chunk
    cut = len(data) // 3
    first = await client.patch(SESSION.format(upload_id=upload_id), content=data[:cut], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert first.status_code == 204, first.text
    assert first.headers["upload-offset"] == str(cut)

    # "connection dropped": the client does not know what arrived, asks the server
    head = await client.head(SESSION.format(upload_id=upload_id), headers=headers(user))
    assert head.status_code == 200 and head.headers["upload-offset"] == str(cut) and head.headers["upload-status"] == "open"

    # a retried chunk with a stale offset is refused and told the real offset
    stale = await client.patch(SESSION.format(upload_id=upload_id), content=data[:cut], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert stale.status_code == 409 and stale.headers["upload-offset"] == str(cut)
    assert stale.json()["offset"] == cut

    # completing too early says how much is missing
    early = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert early.status_code == 409 and early.json()["offset"] == cut

    # a chunk that would overrun the declared size is refused without touching the file
    overrun = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:] + b"x", headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert overrun.status_code == 413
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["received_bytes"] == cut

    # rest of the file in two more chunks
    mid = cut + (len(data) - cut) // 2
    second = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:mid], headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert second.status_code == 204
    third = await client.patch(SESSION.format(upload_id=upload_id), content=data[mid:], headers={**CHUNK, "Upload-Offset": str(mid), **headers(user)})
    assert third.status_code == 204 and third.headers["upload-offset"] == str(len(data))
    status = (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()
    assert status["received_bytes"] == len(data) and status["remaining_bytes"] == 0

    # nothing more can be appended
    extra = await client.patch(SESSION.format(upload_id=upload_id), content=b"y", headers={**CHUNK, "Upload-Offset": str(len(data)), **headers(user)})
    assert extra.status_code == 413

    done = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["upload_id"] == upload_id and result["thumbnail_status"] == "ready" and result["file_size"] == len(data)
    media = await db.get(MediaFile, result["media_file_id"])
    assert media is not None and media.user_id == user.id and media.observation_id == observation.id
    assert media.resolve_path(storage).read_bytes() == data
    assert not (storage / "_tmp" / "uploads" / f"{upload_id}.part").exists(), "part file moved into storage"

    # completion is idempotent, then the session is closed for chunks and abort
    again = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert again.status_code == 200 and again.json()["media_file_id"] == result["media_file_id"]
    closed = await client.patch(SESSION.format(upload_id=upload_id), content=b"z", headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert closed.status_code == 410 and closed.json()["session_status"] == "completed"
    assert (await client.post(ABORT.format(upload_id=upload_id), headers=headers(user))).status_code == 409

    listing = await client.get(CREATE, params={"status": "completed"}, headers=headers(user))
    assert [s["id"] for s in listing.json()] == [upload_id]


@pytest.mark.asyncio
async def test_validation_before_any_byte(client, db, headers, storage):
    post, user, observation = await _scene(db)
    data = make_png()
    no_create = await make_user(db, perms=["media.view"], post_id=post.id)
    assert (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(no_create))).status_code == 403

    bad_format = await client.post(CREATE, json=_create_body(observation.id, data, original_filename="scan.bmp"), headers=headers(user))
    assert bad_format.status_code == 415 and "accept-post" in bad_format.headers

    too_big = await client.post(CREATE, json=_create_body(observation.id, data, expected_size=10 ** 12), headers=headers(user))
    assert too_big.status_code == 413

    missing_obs = await client.post(CREATE, json=_create_body(999999, data), headers=headers(user))
    assert missing_obs.status_code == 404

    other_post = await make_post(db)
    outsider = await make_user(db, perms=["media.view", "media.create"], post_id=other_post.id)
    assert (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(outsider))).status_code == 403


@pytest.mark.asyncio
async def test_ownership_and_headers(client, db, headers, storage):
    post, user, observation = await _scene(db)
    data = make_png()
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]

    colleague = await make_user(db, perms=["media.view", "media.create"], post_id=post.id)
    denied = await client.get(SESSION.format(upload_id=upload_id), headers=headers(colleague))
    assert denied.status_code == 403 and denied.json()["code"] == "upload_session_not_owner"
    manager = await make_user(db, perms=["media.view", "media.create", "media.manage_any"], post_id=post.id)
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(manager))).status_code == 200
    assert (await client.get(SESSION.format(upload_id="0" * 32), headers=headers(user))).status_code == 404

    no_offset = await client.patch(SESSION.format(upload_id=upload_id), content=data, headers={**CHUNK, **headers(user)})
    assert no_offset.status_code == 400
    wrong_type = await client.patch(SESSION.format(upload_id=upload_id), content=data, headers={"Content-Type": "image/png", "Upload-Offset": "0", **headers(user)})
    assert wrong_type.status_code == 415
    empty = await client.patch(SESSION.format(upload_id=upload_id), content=b"", headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert empty.status_code == 400


@pytest.mark.asyncio
async def test_abort_sha_mismatch_and_expiry(client, db, headers, storage, monkeypatch):
    post, user, observation = await _scene(db)
    data = make_png()

    # abort removes the part file
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    await client.patch(SESSION.format(upload_id=upload_id), content=data[:10], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    part = storage / "_tmp" / "uploads" / f"{upload_id}.part"
    assert part.exists()
    aborted = await client.post(ABORT.format(upload_id=upload_id), headers=headers(user))
    assert aborted.status_code == 200 and aborted.json()["status"] == "aborted" and not part.exists()
    assert (await client.patch(SESSION.format(upload_id=upload_id), content=data[10:], headers={**CHUNK, "Upload-Offset": "10", **headers(user)})).status_code == 410

    # wrong sha256 aborts at completion
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data, sha256="ab" * 32), headers=headers(user))).json()["id"]
    await client.patch(SESSION.format(upload_id=upload_id), content=data, headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    mismatch = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert mismatch.status_code == 422 and mismatch.json()["actual_sha256"] == hashlib.sha256(data).hexdigest()
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["status"] == "aborted"

    # expiry: GET reports it, PATCH refuses, the sweep cleans others
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    await client.patch(SESSION.format(upload_id=upload_id), content=data[:10], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    row = await uploads.get_session_row(db, upload_id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.add(row)
    await db.commit()
    expired = await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))
    assert expired.json()["status"] == "expired"
    assert (await client.patch(SESSION.format(upload_id=upload_id), content=data[10:], headers={**CHUNK, "Upload-Offset": "10", **headers(user)})).status_code == 410

    other = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    row = await uploads.get_session_row(db, other)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.add(row)
    await db.commit()
    counts = await uploads.expire_sessions(db, storage)
    assert counts["expired"] == 1
    assert (await uploads.get_session_row(db, other)).status == "expired"

    # a completed-and-then-corrupted format at completion time aborts the session with 415
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    fake = b"MZ" + b"\x00" * (len(data) - 2)
    await client.patch(SESSION.format(upload_id=upload_id), content=fake, headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    bad = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert bad.status_code in (400, 415), bad.text
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["status"] == "aborted"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, status_code, code", [
    (ClientDisconnect(), 400, "upload_chunk_not_stored"),
    (OSError(28, "No space left on device"), 400, "upload_chunk_not_stored"),
    (RuntimeError("a bug, not a broken transfer"), 500, "internal_error"),
])
async def test_broken_chunk_is_rolled_back(client, db, headers, storage, monkeypatch, failure, status_code, code):
    """Only a broken transfer or a refusing disk is upload_chunk_not_stored; a bug is a plain 500. Both roll back"""
    post, user, observation = await _scene(db)
    data = make_png(400, 300)
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    cut = len(data) // 2
    first = await client.patch(SESSION.format(upload_id=upload_id), content=data[:cut], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert first.status_code == 204

    real_append = uploads.append_chunk

    async def breaks_after_some_bytes(body):
        async for piece in body:
            yield piece[:10]
            break
        raise failure

    async def append_then_fail(session, row, token, root, body, offset, max_chunk):
        return await real_append(session, row, token, root, breaks_after_some_bytes(body), offset, max_chunk)

    monkeypatch.setattr(uploads, "append_chunk", append_then_fail)
    broken = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:], headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert broken.status_code == status_code and broken.json()["code"] == code, broken.text
    monkeypatch.setattr(uploads, "append_chunk", real_append)

    # the 10 bytes written before the failure are gone and the writer is free again
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["received_bytes"] == cut
    assert (storage / "_tmp" / "uploads" / f"{upload_id}.part").stat().st_size == cut
    rest = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:], headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert rest.status_code == 204 and rest.headers["upload-offset"] == str(len(data))


@pytest.mark.asyncio
async def test_a_database_error_mid_chunk_frees_the_session_at_once(client, db, headers, storage, monkeypatch):
    """A failure of the request's database session while a chunk streams is rolled back before the writer cleans
    up: the bytes are truncated away and the next PATCH at the same offset is taken at once, not after
    WRITER_STALE_SECONDS"""
    _, user, observation = await _scene(db)
    data = make_png(400, 300)
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    cut = len(data) // 2
    first = await client.patch(SESSION.format(upload_id=upload_id), content=data[:cut], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert first.status_code == 204

    real_append = uploads.append_chunk

    async def database_fails_after_some_bytes(session, body):
        async for piece in body:
            yield piece[:10]
            break
        session.add(MediaFile())  # NOT NULL columns left empty: the commit fails, the session needs a rollback
        await session.commit()

    async def append_then_fail(session, row, token, root, body, offset, max_chunk):
        return await real_append(session, row, token, root, database_fails_after_some_bytes(session, body), offset, max_chunk)

    monkeypatch.setattr(uploads, "append_chunk", append_then_fail)
    broken = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:], headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert broken.status_code == 500 and broken.json()["code"] == "internal_error", broken.text
    monkeypatch.setattr(uploads, "append_chunk", real_append)

    assert (storage / "_tmp" / "uploads" / f"{upload_id}.part").stat().st_size == cut
    rest = await client.patch(SESSION.format(upload_id=upload_id), content=data[cut:], headers={**CHUNK, "Upload-Offset": str(cut), **headers(user)})
    assert rest.status_code == 204, rest.text
    assert rest.headers["upload-offset"] == str(len(data))


@pytest.mark.asyncio
async def test_claim_writer_is_exclusive(db, storage):
    post, user, observation = await _scene(db)
    row = uploads.new_session(user.id, observation.id, post.id, "image", "a.png", 100, "image/png", None)
    db.add(row)
    await db.commit()
    token = await uploads.claim_writer(db, row.id, 0)
    with pytest.raises(uploads.WriterBusy):
        await uploads.claim_writer(db, row.id, 0)
    with pytest.raises(uploads.OffsetMismatch):
        await uploads.claim_writer(db, row.id, 5)
    await uploads.release_writer(db, row.id, token, 40)
    assert (await uploads.get_session_row(db, row.id)).received_bytes == 40
    assert await uploads.claim_writer(db, row.id, 40)


@pytest.mark.asyncio
async def test_session_list_scope(client, db, headers, storage):
    """media.manage_any lists other users' sessions at the posts where it is in effect: every post when held at the
    root, the home post of an admin.true holder too (admin.true no longer widens the list)"""
    post_a, user_a, observation_a = await _scene(db)
    post_b, user_b, observation_b = await _scene(db)
    data = make_png()
    id_a = (await client.post(CREATE, json=_create_body(observation_a.id, data), headers=headers(user_a))).json()["id"]
    id_b = (await client.post(CREATE, json=_create_body(observation_b.id, data), headers=headers(user_b))).json()["id"]

    async def listed(user):
        response = await client.get(CREATE, headers=headers(user))
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()}

    manager = await make_user(db, perms=["media.view", "media.create", "media.manage_any"], post_id=post_a.id)
    administrator = await make_user(db, perms=["admin.true", "media.view", "media.create", "media.manage_any"], post_id=post_a.id)
    assert await listed(user_a) == {id_a}
    assert await listed(manager) == {id_a}
    assert await listed(administrator) == {id_a}
    assert await listed(await make_admin(db)) == {id_a, id_b}


@pytest.mark.asyncio
async def test_upload_result_is_built_from_the_stored_row(db, storage):
    """Every upload route answers the same body, built by the v1 response module from the stored media row"""
    from app.backend.api.v1.models.response.media import UploadResultResponse, upload_result
    from app.backend.enums.general import UploadTransport
    from tests.helpers import make_media

    _, user, observation = await _scene(db)
    media = await make_media(db, observation, user)
    result = upload_result(media, UploadTransport.RESUMABLE, upload_id="abc")
    assert isinstance(result, UploadResultResponse)
    assert result.model_dump(exclude={"thumbnail_urls"}) == {
        "message": "File uploaded successfully (resumable)", "media_file_id": media.id,
        "original_filename": media.original_filename, "file_size": media.file_size, "mime_type": media.mime_type,
        "thumbnail_status": media.thumbnail_status, "upload_id": "abc",
    }
    assert result.thumbnail_urls is not None


# ---------------------------------------------------------------------------
# One request at a time per session: completion claim, writer ownership
# ---------------------------------------------------------------------------

class FakeClock:
    """uploads.utcnow under test control: races are driven by moving the clock, never by sleeping"""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(uploads, "utcnow", fake)
    return fake


async def _received_session(client, headers, user, observation, data: bytes, **overrides) -> str:
    """A session that holds every byte of data, ready to complete"""
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data, **overrides), headers=headers(user))).json()["id"]
    sent = await client.patch(SESSION.format(upload_id=upload_id), content=data, headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert sent.status_code == 204, sent.text
    return upload_id


async def _media_create_audits(db) -> int:
    return len((await db.exec(select(AuditLog).where(AuditLog.action == "create", AuditLog.resource_type == "media"))).all())


async def _outcome(awaitable):
    """The result, or the exception the awaitable raised"""
    try:
        return await awaitable
    except Exception as exc:
        return exc


async def _body(*steps):
    """Request body of a test: bytes are pieces, an async callable runs between pieces, an exception is raised"""
    for step in steps:
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            await step()
            continue
        yield step


@pytest.mark.asyncio
async def test_concurrent_complete_stores_once(client, db, headers, storage, monkeypatch):
    """While one complete stores the file the session is `completing`: a second complete, a chunk and an abort
    answer 409 upload_session_completing and change nothing; the first one stores one file with one audit row"""
    _, user, observation = await _scene(db)
    data = make_png()
    upload_id = await _received_session(client, headers, user, observation, data)
    manager = get_media_manager()
    real_store = manager.store_file_from_path
    during = {}

    async def store_while_others_arrive(**kwargs):
        if not during:
            during["status"] = (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["status"]
            during["complete"] = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
            during["chunk"] = await client.patch(SESSION.format(upload_id=upload_id), content=b"x",
                                                 headers={**CHUNK, "Upload-Offset": str(len(data)), **headers(user)})
            during["abort"] = await client.post(ABORT.format(upload_id=upload_id), headers=headers(user))
        return await real_store(**kwargs)

    monkeypatch.setattr(manager, "store_file_from_path", store_while_others_arrive)
    done = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert done.status_code == 200, done.text
    assert during["status"] == "completing"
    for name in ("complete", "chunk", "abort"):
        assert during[name].status_code == 409, (name, during[name].text)
        assert during[name].json()["code"] == "upload_session_completing", name
        assert during[name].headers["upload-status"] == "completing", name

    final = (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()
    assert final["status"] == "completed" and final["media_file_id"] == done.json()["media_file_id"] and final["error"] is None
    stored = (await db.exec(select(MediaFile).where(MediaFile.observation_id == observation.id))).all()
    assert [m.id for m in stored] == [done.json()["media_file_id"]]
    assert await _media_create_audits(db) == 1


@pytest.mark.asyncio
async def test_a_crashed_completion_blocks_for_a_limited_time(client, db, headers, storage, clock):
    """A completion claim nobody releases (the request died) is taken over after COMPLETION_STALE_SECONDS"""
    _, user, observation = await _scene(db)
    data = make_png()
    upload_id = await _received_session(client, headers, user, observation, data)
    crashed = await uploads.claim_completion(db, upload_id)
    assert crashed

    busy = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert busy.status_code == 409 and busy.json()["code"] == "upload_session_completing"
    clock.advance(uploads.COMPLETION_STALE_SECONDS - 1)
    assert (await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))).status_code == 409

    clock.advance(2)
    done = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert done.status_code == 200, done.text
    # the crashed request can no longer change the session
    assert await uploads.release_completion(db, upload_id, crashed) is False
    assert (await uploads.get_session_row(db, upload_id)).status == "completed"


@pytest.mark.asyncio
async def test_expiry_spares_a_live_completion(db, storage, clock):
    """The sweep expires an open session or a stale completion claim, never a completion that is running"""
    _, user, observation = await _scene(db)
    row = uploads.new_session(user.id, observation.id, observation.post_id, "image", "a.png", 0, "image/png", None)
    db.add(row)
    await db.commit()
    assert await uploads.claim_completion(db, row.id)
    row = await uploads.get_session_row(db, row.id)
    row.expires_at = clock.now - timedelta(minutes=1)
    db.add(row)
    await db.commit()

    assert (await uploads.expire_sessions(db, storage))["expired"] == 0
    assert (await uploads.get_session_row(db, row.id)).status == "completing"
    clock.advance(uploads.COMPLETION_STALE_SECONDS + 1)
    assert (await uploads.expire_sessions(db, storage))["expired"] == 1
    assert (await uploads.get_session_row(db, row.id)).status == "expired"


async def _storage_busy(**kwargs):
    raise CodedError(ErrorCode.MEDIA_UPLOADS_BUSY)


async def _a_bug(**kwargs):
    raise RuntimeError("a bug, not a refused file")


async def _database_error(session, **kwargs):
    """A failure of the request's own database session: the commit fails and the session needs a rollback"""
    session.add(MediaFile())  # NOT NULL columns left empty
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("store, status_code, code", [
    (_storage_busy, 503, "media_uploads_busy"),
    (_a_bug, 500, "internal_error"),
    (_database_error, 500, "internal_error"),
], ids=["busy", "bug", "database-error"])
async def test_a_server_side_failure_at_completion_keeps_the_session_open(
        client, db, headers, storage, monkeypatch, store, status_code, code):
    """Busy storage, a bug or a database error releases the completion claim: the session is open again and
    complete can be retried"""
    _, user, observation = await _scene(db)
    data = make_png()
    upload_id = await _received_session(client, headers, user, observation, data)
    manager = get_media_manager()
    real_store = manager.store_file_from_path

    monkeypatch.setattr(manager, "store_file_from_path", store)
    refused = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert refused.status_code == status_code and refused.json()["code"] == code, refused.text
    state = (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()
    assert state["status"] == "open" and state["error"] is None
    monkeypatch.setattr(manager, "store_file_from_path", real_store)
    assert (await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))).status_code == 200


async def _open_row(db, size: int = 100):
    _, user, observation = await _scene(db)
    row = uploads.new_session(user.id, observation.id, observation.post_id, "image", "a.png", size, "image/png", None)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("after_takeover", [b"a" * 10, ClientDisconnect()], ids=["sends-more", "breaks"])
async def test_a_writer_that_lost_its_token_never_touches_the_session(db, storage, clock, after_takeover):
    """Writer A stalls past WRITER_STALE_SECONDS, writer B takes the session and stores its chunk. Whatever A
    does next (more bytes or a broken transfer), it writes nothing, truncates nothing and releases nothing"""
    row = await _open_row(db)
    upload_id = row.id  # a failed chunk rolls the session back, which expires the row
    part = uploads.resolve_part(row, storage)
    token_a = await uploads.claim_writer(db, upload_id, 0)

    async def b_takes_over():
        clock.advance(uploads.WRITER_STALE_SECONDS + 1)
        token_b = await uploads.claim_writer(db, upload_id, 0)
        assert await uploads.append_chunk(db, row, token_b, storage, _body(b"b" * 30), 0, 1000) == 30

    outcome = await _outcome(uploads.append_chunk(db, row, token_a, storage, _body(b"a" * 10, b_takes_over, after_takeover), 0, 1000))
    assert part.read_bytes() == b"b" * 30
    fresh = await uploads.get_session_row(db, upload_id)
    assert fresh.received_bytes == 30 and fresh.writer_token is None and fresh.status == "open"
    expected = uploads.WriterLost if isinstance(after_takeover, bytes) else ClientDisconnect
    assert isinstance(outcome, expected), repr(outcome)


@pytest.mark.asyncio
async def test_a_slow_writer_keeps_its_token_while_it_streams(db, storage, clock):
    """A writer renews writer_since as bytes arrive: a chunk taking longer than WRITER_STALE_SECONDS in total
    keeps the session, a competing claim answers WriterBusy"""
    row = await _open_row(db)
    token = await uploads.claim_writer(db, row.id, 0)
    competitor = {}
    pause = uploads.WRITER_STALE_SECONDS - 80

    async def waits():
        clock.advance(pause)

    async def competes():
        clock.advance(pause)
        competitor["outcome"] = await _outcome(uploads.claim_writer(db, row.id, 0))

    new_offset = await uploads.append_chunk(
        db, row, token, storage, _body(b"a" * 10, waits, b"a" * 10, competes, b"a" * 10), 0, 1000)
    assert isinstance(competitor["outcome"], uploads.WriterBusy), repr(competitor["outcome"])
    assert new_offset == 30 and uploads.resolve_part(row, storage).read_bytes() == b"a" * 30
    assert (await uploads.get_session_row(db, row.id)).received_bytes == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["shortened", "deleted"])
async def test_a_part_file_shorter_than_received_bytes_aborts(client, db, headers, storage, damage):
    """The next chunk never fills a gap with zeros: the session is aborted with upload_bytes_missing"""
    _, user, observation = await _scene(db)
    data = make_png(400, 300)
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    first = await client.patch(SESSION.format(upload_id=upload_id), content=data[:20], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert first.status_code == 204
    part = storage / "_tmp" / "uploads" / f"{upload_id}.part"
    if damage == "shortened":
        part.write_bytes(data[:5])
    else:
        part.unlink()

    refused = await client.patch(SESSION.format(upload_id=upload_id), content=data[20:40], headers={**CHUNK, "Upload-Offset": "20", **headers(user)})
    assert refused.status_code == 410, refused.text
    assert refused.json()["code"] == "upload_bytes_missing" and refused.json()["session_status"] == "aborted"
    assert not part.exists()
    state = (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()
    assert state["status"] == "aborted" and state["error"] == "upload_bytes_missing"


@pytest.mark.asyncio
async def test_repeat_complete_returns_the_stored_result_without_an_audit_row(client, db, headers, storage):
    _, user, observation = await _scene(db)
    data = make_png()
    upload_id = await _received_session(client, headers, user, observation, data)
    first = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert first.status_code == 200, first.text
    assert await _media_create_audits(db) == 1

    again = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert again.status_code == 200
    assert again.json()["media_file_id"] == first.json()["media_file_id"] and again.json()["upload_id"] == upload_id
    assert await _media_create_audits(db) == 1


@pytest.mark.asyncio
async def test_session_error_is_the_problem_code_that_ended_it(client, db, headers, storage):
    """error carries the code of the problem that ended the session, never an internal reason; a client abort and
    an expiry end a session without a problem (null), and text stored by older versions is not answered"""
    _, user, observation = await _scene(db)
    data = make_png()

    async def state(upload_id):
        return (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()

    mismatch = await _received_session(client, headers, user, observation, data, sha256="ab" * 32)
    assert (await client.post(COMPLETE.format(upload_id=mismatch), headers=headers(user))).status_code == 422
    assert (await state(mismatch))["error"] == "upload_checksum_mismatch"

    rejected = await _received_session(client, headers, user, observation, b"MZ" + bytes(len(data) - 2))
    refused = await client.post(COMPLETE.format(upload_id=rejected), headers=headers(user))
    assert refused.status_code in (400, 415), refused.text
    assert (await state(rejected))["error"] == refused.json()["code"]

    aborted = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    assert (await client.post(ABORT.format(upload_id=aborted), headers=headers(user))).json()["error"] is None

    expired = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    row = await uploads.get_session_row(db, expired)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.add(row)
    await db.commit()
    assert (await state(expired))["status"] == "expired" and (await state(expired))["error"] is None

    legacy = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    row = await uploads.get_session_row(db, legacy)
    row.status, row.error = "aborted", "media_content_invalid: {'detail': '/srv/media/_tmp/uploads/x.part'}"
    db.add(row)
    await db.commit()
    assert (await state(legacy))["error"] is None
    listed = (await client.get(CREATE, params={"status": "aborted"}, headers=headers(user))).json()
    assert {s["id"]: s["error"] for s in listed} == {
        mismatch: "upload_checksum_mismatch", rejected: refused.json()["code"], aborted: None, legacy: None}


@pytest.mark.asyncio
async def test_head_applies_the_expiry_like_get(client, db, headers, storage, clock):
    _, user, observation = await _scene(db)
    data = make_png()
    upload_id = (await client.post(CREATE, json=_create_body(observation.id, data), headers=headers(user))).json()["id"]
    first = await client.patch(SESSION.format(upload_id=upload_id), content=data[:10], headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert first.status_code == 204
    part = storage / "_tmp" / "uploads" / f"{upload_id}.part"
    clock.advance(settings.storage.upload.session_ttl_hours * 3600 + 60)

    head = await client.head(SESSION.format(upload_id=upload_id), headers=headers(user))
    assert head.status_code == 200 and head.headers["upload-status"] == "expired"
    assert not part.exists()
    assert (await client.get(SESSION.format(upload_id=upload_id), headers=headers(user))).json()["status"] == "expired"


@pytest.mark.asyncio
async def test_resumable_completion_audit_row_names_the_session(client, db, headers, storage):
    """media.create of a resumable upload: scope of the post, via and upload_id in params"""
    _, user, observation = await _scene(db)
    upload_id = await _received_session(client, headers, user, observation, make_png())
    assert (await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))).status_code == 200
    row = (await db.exec(select(AuditLog).where(AuditLog.event == "media.create"))).one()
    assert row.scope_key == f"p:{observation.post_id}"
    assert row.params["via"] == "resumable" and row.params["upload_id"] == upload_id
    assert row.params["observation_id"] == observation.id
