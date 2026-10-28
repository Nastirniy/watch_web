"""Accepted media formats: the managed registry, its admin CRUD, GET /media/formats, and 415 / 413 answers."""
import pytest
from sqlmodel import select

from app.backend.config import settings
from app.backend.enums.general import UploadPurpose
from app.backend.enums.routes import MediaRoutes, MediaFormatRoutes
from app.backend.api.v1 import V1
from app.backend.models.media_format import MediaFormat
from app.backend.services.files import formats
from app.backend.auth.scope import ROOT
from tests.helpers import assign, give, make_admin, make_dictionary, make_observation, make_post, make_role, make_user

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
    assert registry.media_types() == ["image", "video", "audio", "document"]
    assert registry.content_types("image") == ["image/jpeg", "image/png", "image/webp", "image/gif"]
    assert ".bmp" not in registry.extensions("image") and ".avi" not in registry.extensions("video")
    canonical = registry.content_types()
    assert len(canonical) == len(set(canonical)), "no duplicate Content-Types"
    all_ext = registry.extensions()
    assert len(all_ext) == len(set(all_ext)), "an extension maps to one format only"
    assert registry.accept_post_header("video") == "video/mp4, video/webm, video/quicktime, video/x-matroska"
    observation = UploadPurpose.OBSERVATION_MEDIA
    assert registry.resolve(observation, "clip.MOV").content_type == "video/quicktime"
    assert registry.resolve(observation, "clip") is None
    jpeg = registry.resolve(observation, "a.jpg")
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
async def test_nginx_upload_checks_declared_format_before_the_temp_file(client, db, headers, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(tmp_path))
    temp_file = tmp_path / "0000000001"
    temp_file.write_bytes(b"not read")
    user, observation = await _scene(db)
    response = await client.post(
        NGINX_UPLOAD, params={"observation_id": observation.id, "media_type": "video"},
        headers={"X-File-Name": str(temp_file), "X-Original-Filename": "clip.avi", **headers(user)},
        content=b"",
    )
    assert response.status_code == 415, response.text
    assert response.headers["accept-post"].startswith("video/mp4")
    assert not temp_file.exists()


# ----------------------------------------------------------------- admin CRUD

@pytest.mark.asyncio
async def test_media_format_crud(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db, post_id=post.id)
    viewer = await make_user(db, perms=["media_format.view"], post_id=post.id)
    granular = await make_user(db, perms=["media_format.view", "media_format.create", "media_format.update", "media_format.archive"], post_id=post.id)

    listing = await client.get(LIST, headers=headers(viewer))
    assert listing.status_code == 200 and len(listing.json()) == 23
    assert (await client.get(LIST, params={"include_deleted": "true"}, headers=headers(viewer))).status_code == 403
    assert (await client.get(LIST, params={"media_type": "audio"}, headers=headers(viewer))).json().__len__() == 5

    payload = {"media_type": "image", "content_type": "image/avif", "label": "AVIF", "extensions": ["avif", ".AVIF"],
               "aliases": ["image/avif-sequence"], "max_file_size_bytes": 20 * formats.MB, "sort_order": 9}
    assert (await client.post(CREATE, json=payload, headers=headers(viewer))).status_code == 403
    denied = await client.post(CREATE, json=payload, headers=headers(granular))
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    assert denied.json()["missing"] == ["media_format.create"]

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
async def test_restoring_through_create_needs_the_archive_permission(client, db, headers):
    """Creating an archived content_type again restores it, and a restore needs media_format.archive (as POST
    /restore does): with view and create alone it answered 200 and restored the row (live test, 2026-10-19)"""
    admin = await make_admin(db)
    # type admin (its ceiling allows every permission); the role at the root is all each user holds
    creator = await make_user(db, user_type="system_administrator")
    await give(db, creator, ["media_format.view", "media_format.create"])
    archiver = await make_user(db, user_type="system_administrator")
    await give(db, archiver, ["media_format.view", "media_format.create", "media_format.archive"])
    payload = {"media_type": "image", "content_type": "image/avif", "label": "AVIF", "extensions": [".avif"],
               "max_file_size_bytes": 20 * formats.MB}
    created = await client.post(CREATE, json=payload, headers=headers(creator))
    assert created.status_code == 200, "a new content type needs only create"
    format_id = created.json()["id"]
    assert (await client.post(ARCHIVE.format(format_id=format_id), headers=headers(admin))).status_code == 200

    refused = await client.post(CREATE, json=dict(payload, label="AVIF again"), headers=headers(creator))
    assert refused.status_code == 403 and refused.json()["code"] == "permission_denied"
    assert refused.json()["missing"] == ["media_format.archive"]
    assert "media_format.archive" in refused.json()["required"]["all"]
    row = (await db.exec(select(MediaFormat).where(MediaFormat.id == format_id))).one()
    await db.refresh(row)
    assert row.is_deleted is True and row.label == "AVIF", "nothing was restored"

    restored = await client.post(CREATE, json=dict(payload, label="AVIF again"), headers=headers(archiver))
    assert restored.status_code == 200 and restored.json()["id"] == format_id and restored.json()["is_deleted"] is False


@pytest.mark.asyncio
async def test_media_formats_searchable(client, db, headers):
    from app.backend.enums.routes import SearchRoutes
    post = await make_post(db)
    viewer = await make_user(db, perms=["media_format.view"], post_id=post.id)
    response = await client.get(V1.path(SearchRoutes.MEDIA_FORMATS), params={"media_type": "video"}, headers=headers(viewer))
    assert response.status_code == 200, response.text
    items = response.json().get("items", response.json())
    assert len(items) == 4


# ----------------------------------------------------------------- seeding, cache generation, ceiling

@pytest.mark.asyncio
async def test_seeding_skips_a_default_whose_extension_or_alias_belongs_to_another_row(db):
    jpeg = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/jpeg"))).one()
    jpeg.content_type = "image/x-photo"          # an admin renamed the default
    await db.commit()

    assert await formats.seed_media_formats(db) == 0
    owners = (await db.exec(select(MediaFormat).where(MediaFormat.is_deleted == False))).all()
    jpg_owners = [row.content_type for row in owners if ".jpg" in (row.extensions or [])]
    assert jpg_owners == ["image/x-photo"]
    assert "image/jpeg" not in {row.content_type for row in owners}
    assert await formats.seed_media_formats(db) == 0

    # an alias clash is skipped as well
    png = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/png"))).one()
    png.content_type = "image/x-png"
    png.aliases = ["image/webp"]
    await db.commit()
    await db.refresh(png)
    webp = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/webp"))).one()
    webp.content_type = "image/x-webp"
    webp.extensions = [".wbp"]
    await db.commit()
    assert await formats.seed_media_formats(db) == 0   # image/webp is an alias of image/x-png now


class _InvalidatingSession:
    """Runs a write's invalidate_registry_cache() while the registry read is in flight"""

    def __init__(self, session):
        self._session = session

    async def exec(self, *args, **kwargs):
        result = await self._session.exec(*args, **kwargs)
        formats.invalidate_registry_cache()
        return result


@pytest.mark.asyncio
async def test_registry_read_before_an_invalidate_is_not_cached(db):
    formats.invalidate_registry_cache()
    stale = await formats.load_registry(_InvalidatingSession(db))
    assert "image/png" in stale.content_types()
    assert formats._cache[1] is None, "a registry read before the invalidate must not be stored"

    png = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/png"))).one()
    png.is_active = False
    await db.commit()
    fresh = await formats.load_registry(db)
    assert "image/png" not in fresh.content_types()
    assert formats._cache[1] is fresh


@pytest.mark.asyncio
async def test_lowered_ceiling_caps_every_limit(client, db, headers, monkeypatch):
    user, observation = await _scene(db)
    mp4 = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "video/mp4"))).one()
    mp4.max_file_size_bytes = 5 * formats.GB
    await db.commit()
    formats.invalidate_registry_cache()
    monkeypatch.setattr(settings.storage, "max_file_size_gb", 1)

    registry = await formats.load_registry(db, use_cache=False)
    spec = registry.resolve(UploadPurpose.OBSERVATION_MEDIA, "clip.mp4")
    assert spec.max_file_size_bytes == 5 * formats.GB and spec.effective_limit_bytes == formats.GB
    from app.backend.api.shared.helpers.upload_helper import validation_config_for
    assert validation_config_for(registry, spec).max_size == formats.GB

    listed = (await client.get(FORMATS, headers=headers(user))).json()
    assert listed["max_file_size_bytes"] == formats.GB
    assert all(f["max_file_size_bytes"] <= formats.GB for group in listed["formats"].values() for f in group)

    created = await client.post(V1.path(MediaRoutes.UPLOADS), headers=headers(user), json={
        "observation_id": observation.id, "media_type": "video", "original_filename": "clip.mp4",
        "expected_size": 2 * formats.GB, "content_type": "video/mp4"})
    assert created.status_code == 413, created.text
    assert created.json()["max_file_size_bytes"] == formats.GB

    accepted = await client.post(V1.path(MediaRoutes.UPLOADS), headers=headers(user), json={
        "observation_id": observation.id, "media_type": "video", "original_filename": "clip.mp4",
        "expected_size": formats.GB, "content_type": "video/mp4"})
    assert accepted.status_code == 201, accepted.text
