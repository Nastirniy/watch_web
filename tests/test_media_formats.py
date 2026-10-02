"""Accepted media formats: the managed registry, its admin CRUD, GET /media/formats, and 415 / 413 answers."""
import pytest
from sqlmodel import select

from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes, MediaFormatRoutes
from app.backend.api.v1 import V1
from app.backend.models.media_format import MediaFormat
from app.backend.services.media import formats
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation

UPLOAD = V1.path(MediaRoutes.UPLOAD)
NGINX_UPLOAD = V1.path(MediaRoutes.NGINX_UPLOAD)
FORMATS = V1.path(MediaRoutes.FORMATS)
LIST = V1.path(MediaFormatRoutes.LIST_ALL)
CREATE = V1.path(MediaFormatRoutes.CREATE)
BY_ID = V1.path(MediaFormatRoutes.FORMAT_BY_ID)
ARCHIVE = V1.path(MediaFormatRoutes.FORMAT_ARCHIVE)
RESTORE = V1.path(MediaFormatRoutes.FORMAT_RESTORE)

PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
             + b"\x90wS\xde" + b"\x00\x00\x00\x00IEND\xaeB`\x82")


# ----------------------------------------------------------------- registry

@pytest.mark.asyncio
async def test_seeded_registry_is_limited_and_consistent(db):
    registry = await formats.load_registry(db, use_cache=False)
    assert registry.media_types() == ["image", "video", "audio"]
    assert registry.content_types("image") == ["image/jpeg", "image/png", "image/webp", "image/gif"]
    assert ".bmp" not in registry.extensions("image") and ".avi" not in registry.extensions("video")
    canonical = registry.content_types()
    assert len(canonical) == len(set(canonical)), "no duplicate Content-Types"
    all_ext = registry.extensions()
    assert len(all_ext) == len(set(all_ext)), "an extension maps to one format only"
    assert registry.accept_post_header("video") == "video/mp4, video/webm, video/quicktime, video/x-matroska"
    assert registry.media_type_for_mime("audio/x-wav") == "audio"
    assert registry.is_accepted_content_type("image", "application/octet-stream")
    assert registry.is_accepted_content_type("image", "IMAGE/JPEG; charset=binary")
    assert not registry.is_accepted_content_type("image", "video/mp4")
    assert registry.resolve("video", "clip.MOV").content_type == "video/quicktime"
    assert registry.resolve("video", "clip") is None
    jpeg = registry.resolve("image", "a.jpg")
    assert jpeg.max_file_size_bytes == 50 * formats.MB and jpeg.id is not None
    # seeding again adds nothing and never edits rows
    assert await formats.seed_media_formats(db) == 0


async def _scene(db, perms=("media.view", "media.create")):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=list(perms), post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return user, observation


# ----------------------------------------------------------------- public endpoint

@pytest.mark.asyncio
async def test_formats_endpoint(client, db, headers):
    user, _ = await _scene(db)
    denied = await client.get(FORMATS)
    assert denied.status_code == 401
    response = await client.get(FORMATS, headers=headers(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["media_types"] == ["image", "video", "audio"]
    first = body["formats"]["image"][0]
    assert first["content_type"] == "image/jpeg" and first["extensions"] == [".jpg", ".jpeg"]
    assert first["aliases"] == ["image/pjpeg"] and first["max_file_size_bytes"] == 50 * formats.MB and first["id"]
    assert {f["content_type"] for f in body["formats"]["audio"]} == {"audio/mpeg", "audio/wav", "audio/flac", "audio/ogg", "audio/mp4"}
    assert body["max_file_size_bytes"] == formats.hard_limit_bytes()
    assert response.headers["accept-post"].startswith("image/jpeg, image/png")


# ----------------------------------------------------------------- uploads

@pytest.mark.asyncio
async def test_upload_rejects_formats_outside_the_registry(client, db, headers):
    user, observation = await _scene(db)
    data = {"observation_id": observation.id, "media_type": "image"}

    bmp = await client.post(UPLOAD, data=data, files={"file": ("scan.bmp", b"BM" + b"\x00" * 64, "image/bmp")}, headers=headers(user))
    assert bmp.status_code == 415, bmp.text
    assert bmp.headers["accept-post"] == "image/jpeg, image/png, image/webp, image/gif"
    assert bmp.json()["accepted_extensions"] == [".jpg", ".jpeg", ".png", ".webp", ".gif"]

    wrong_type = await client.post(UPLOAD, data=data, files={"file": ("photo.png", PNG_BYTES, "video/mp4")}, headers=headers(user))
    assert wrong_type.status_code == 415 and wrong_type.json()["code"] == "media_content_type_not_accepted"

    mismatch = await client.post(UPLOAD, data=data, files={"file": ("photo.png", PNG_BYTES, "image/jpeg")}, headers=headers(user))
    assert mismatch.status_code == 415 and mismatch.json()["code"] == "media_content_type_extension_mismatch"

    unknown = await client.post(UPLOAD, data={"observation_id": observation.id, "media_type": "document"},
                                files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")}, headers=headers(user))
    assert unknown.status_code == 415

    exe = await client.post(UPLOAD, data=data, files={"file": ("payload.exe", b"MZ" + b"\x00" * 64, "application/octet-stream")}, headers=headers(user))
    assert exe.status_code == 415

    fake = await client.post(UPLOAD, data=data, files={"file": ("photo.png", b"MZ" + b"\x00" * 64, "image/png")}, headers=headers(user))
    assert fake.status_code in (400, 415), fake.text


@pytest.mark.asyncio
async def test_upload_respects_per_format_size_limit_and_deactivation(client, db, headers):
    user, observation = await _scene(db)
    data = {"observation_id": observation.id, "media_type": "image"}
    png = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/png"))).one()

    png.max_file_size_bytes = 10
    await db.commit()
    formats.invalidate_registry_cache()
    too_big = await client.post(UPLOAD, data=data, files={"file": ("photo.png", PNG_BYTES, "image/png")}, headers=headers(user))
    assert too_big.status_code == 413, too_big.text
    assert too_big.json()["max_file_size_bytes"] == 10 and too_big.json()["content_type"] == "image/png"

    png.is_active = False
    await db.commit()
    formats.invalidate_registry_cache()
    disabled = await client.post(UPLOAD, data=data, files={"file": ("photo.png", PNG_BYTES, "image/png")}, headers=headers(user))
    assert disabled.status_code == 415
    assert "image/png" not in disabled.headers["accept-post"]


@pytest.mark.asyncio
async def test_nginx_upload_checks_declared_format_before_the_temp_file(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    user, observation = await _scene(db)
    response = await client.post(
        NGINX_UPLOAD, params={"observation_id": observation.id, "media_type": "video"},
        headers={"X-File-Name": "/nonexistent/tmp/0000000001", "X-Original-Filename": "clip.avi", **headers(user)},
        content=b"",
    )
    assert response.status_code == 415, response.text
    assert response.headers["accept-post"].startswith("video/mp4")


# ----------------------------------------------------------------- admin CRUD

@pytest.mark.asyncio
async def test_media_format_crud(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db, post_id=post.id)
    viewer = await make_user(db, perms=["media_format.view"], post_id=post.id)
    granular = await make_user(db, perms=["media_format.view", "media_format.create", "media_format.update", "media_format.archive"], post_id=post.id)

    listing = await client.get(LIST, headers=headers(viewer))
    assert listing.status_code == 200 and len(listing.json()) == 13
    assert (await client.get(LIST, params={"include_deleted": "true"}, headers=headers(viewer))).status_code == 403
    assert (await client.get(LIST, params={"media_type": "audio"}, headers=headers(viewer))).json().__len__() == 5

    payload = {"media_type": "image", "content_type": "image/avif", "label": "AVIF", "extensions": ["avif", ".AVIF"],
               "aliases": ["image/avif-sequence"], "max_file_size_bytes": 20 * formats.MB, "sort_order": 9}
    assert (await client.post(CREATE, json=payload, headers=headers(viewer))).status_code == 403
    denied = await client.post(CREATE, json=payload, headers=headers(granular))
    assert denied.status_code == 403 and denied.json()["code"] == "admin_required"

    created = await client.post(CREATE, json=payload, headers=headers(admin))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["extensions"] == [".avif"] and body["aliases"] == ["image/avif-sequence"] and body["is_active"] is True
    format_id = body["id"]

    # the public endpoint and uploads see it immediately
    public = (await client.get(FORMATS, headers=headers(admin))).json()
    assert any(f["content_type"] == "image/avif" for f in public["formats"]["image"])

    # validation: duplicates, clashes, type prefix, ceiling
    assert (await client.post(CREATE, json=payload, headers=headers(admin))).status_code == 400
    clash = dict(payload, content_type="image/avif2", extensions=[".png"])
    assert (await client.post(CREATE, json=clash, headers=headers(admin))).json()["code"] == "media_format_extension_clash"
    alias_clash = dict(payload, content_type="image/avif3", extensions=[".av3"], aliases=["image/pjpeg"])
    assert (await client.post(CREATE, json=alias_clash, headers=headers(admin))).status_code == 400
    wrong_prefix = dict(payload, content_type="video/avif", extensions=[".av4"])
    assert (await client.post(CREATE, json=wrong_prefix, headers=headers(admin))).status_code == 422
    too_big = dict(payload, content_type="image/avif5", extensions=[".av5"], max_file_size_bytes=formats.hard_limit_bytes() + 1)
    assert (await client.post(CREATE, json=too_big, headers=headers(admin))).json()["code"] == "media_format_size_above_ceiling"
    bad_ct = dict(payload, content_type="image/AV IF", extensions=[".av6"])
    assert (await client.post(CREATE, json=bad_ct, headers=headers(admin))).status_code == 422

    # update
    updated = await client.put(BY_ID.format(format_id=format_id), json={"max_file_size_bytes": 5 * formats.MB, "is_active": False}, headers=headers(admin))
    assert updated.status_code == 200 and updated.json()["max_file_size_bytes"] == 5 * formats.MB and updated.json()["is_active"] is False
    assert not any(f["content_type"] == "image/avif" for f in (await client.get(FORMATS, headers=headers(admin))).json()["formats"]["image"])
    assert (await client.put(BY_ID.format(format_id=format_id), json={}, headers=headers(admin))).status_code == 400
    assert (await client.put(BY_ID.format(format_id=format_id), json={"extensions": [".jpg"]}, headers=headers(admin))).status_code == 400

    one = await client.get(BY_ID.format(format_id=format_id), headers=headers(viewer))
    assert one.status_code == 200 and one.json()["label"] == "AVIF"

    # archive, then restore through create and through POST /restore
    assert (await client.delete(BY_ID.format(format_id=format_id), headers=headers(admin))).status_code == 405, "no hard delete for formats"
    deleted = await client.post(ARCHIVE.format(format_id=format_id), headers=headers(admin))
    assert deleted.status_code == 200 and deleted.json()["is_deleted"] is True
    assert (await client.post(ARCHIVE.format(format_id=format_id), headers=headers(admin))).status_code == 404
    assert (await client.get(BY_ID.format(format_id=format_id), headers=headers(viewer))).status_code == 404
    assert (await client.get(BY_ID.format(format_id=format_id), headers=headers(admin))).status_code == 200, "archive holders can inspect archived rows"
    assert (await client.put(BY_ID.format(format_id=format_id), json={"label": "x"}, headers=headers(admin))).status_code == 404, "archived rows are read-only"
    deleted_listing = (await client.get(LIST, params={"include_deleted": "true", "content_type": "image/avif"}, headers=headers(admin))).json()
    assert deleted_listing and deleted_listing[0]["is_deleted"] is True

    restored = await client.post(CREATE, json=dict(payload, label="AVIF again"), headers=headers(admin))
    assert restored.status_code == 200 and restored.json()["id"] == format_id and restored.json()["label"] == "AVIF again"
    assert restored.json()["is_deleted"] is False

    assert (await client.post(RESTORE.format(format_id=format_id), headers=headers(admin))).status_code == 409
    await client.post(ARCHIVE.format(format_id=format_id), headers=headers(admin))
    assert (await client.post(RESTORE.format(format_id=format_id), headers=headers(granular))).status_code == 403, "administration route"
    restored_again = await client.post(RESTORE.format(format_id=format_id), headers=headers(admin))
    assert restored_again.status_code == 200 and restored_again.json()["is_deleted"] is False

    # audit trail exists for the dictionary
    from app.backend.models.audit import AuditLog
    actions = (await db.exec(select(AuditLog.action).where(AuditLog.resource_type == "media_format"))).all()
    assert {"create", "update", "archive", "restore"} <= set(actions)


@pytest.mark.asyncio
async def test_media_formats_searchable(client, db, headers):
    from app.backend.enums.routes import SearchRoutes
    post = await make_post(db)
    viewer = await make_user(db, perms=["media_format.view"], post_id=post.id)
    response = await client.get(V1.path(SearchRoutes.MEDIA_FORMATS), params={"media_type": "video"}, headers=headers(viewer))
    assert response.status_code == 200, response.text
    items = response.json().get("items", response.json())
    assert len(items) == 4
