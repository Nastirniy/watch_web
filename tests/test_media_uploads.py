"""Resumable uploads: session lifecycle, offsets, ownership, completion, abort and expiry."""
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.backend.models.media import MediaFile
from app.backend.services.media import uploads
from app.backend.services.media.manager import reset_media_manager
from tests.helpers import make_post, make_user, make_dictionary, make_observation

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
    """media.manage_any lists other users' sessions: every post for administrators, the data scope otherwise"""
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
    assert await listed(administrator) == {id_a, id_b}
