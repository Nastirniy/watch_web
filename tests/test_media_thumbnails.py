"""Thumbnail, share, regenerate and poster administration routes.

Images are rendered on demand (nothing stored), videos depend on the poster
job state, audio is unsupported. Behind nginx the API answers with
X-Accel-Redirect; without nginx it proxies the renderer, which is stubbed here.
"""
import base64

import httpx
import pytest

from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.backend.models.media import MediaFile
from app.backend.services.media import renderer
from app.backend.services.media.renderer import Rendered
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation, make_media

VIEW = ["media.view"]


def thumb_url(media_id: int) -> str:
    return V1.path(MediaRoutes.MEDIA_THUMBNAIL, media_id=media_id)


def decode_source(accel_path: str) -> str:
    """local:/// source encoded in the last path segment of an imgproxy path"""
    segment = accel_path.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    return tmp_path


async def _scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, user, observation


# ----------------------------------------------------------------- images

@pytest.mark.asyncio
async def test_image_thumbnail_behind_nginx_redirects_to_imgproxy(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    _, user, observation = await _scene(db)
    media = await make_media(db, observation, user)

    response = await client.get(thumb_url(media.id), headers=headers(user))
    assert response.status_code == 200, response.text
    accel = response.headers["x-accel-redirect"]
    assert accel.startswith(settings.storage.imgproxy.nginx_internal_prefix + "/")
    assert "/rs:fit:300:300:0:0/" in accel
    assert decode_source(accel) == f"local:///{media.file_path}"
    assert response.headers["x-thumbnail-status"] == "ready"
    assert "content-type" not in {k.lower() for k in response.headers if k.lower() == "content-type" and response.headers[k].startswith("image")}, \
        "the API must not pin a Content-Type: nginx keeps it and imgproxy negotiates WebP"

    preview = await client.get(thumb_url(media.id), params={"size": "preview", "format": "webp"}, headers=headers(user))
    assert "/rs:fit:1600:1600:0:0/" in preview.headers["x-accel-redirect"]
    assert preview.headers["x-accel-redirect"].endswith(".webp")

    bad = await client.get(thumb_url(media.id), params={"size": "huge"}, headers=headers(user))
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_image_thumbnail_without_nginx_proxies_the_renderer(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    _, user, observation = await _scene(db)
    media = await make_media(db, observation, user)
    seen = {}

    async def fake_fetch(url):
        seen["url"] = url
        return Rendered(200, {"content-type": "image/webp", "etag": '"abc"'}, b"webp-bytes")

    monkeypatch.setattr(renderer, "fetch", fake_fetch)
    response = await client.get(thumb_url(media.id), headers=headers(user))
    assert response.status_code == 200
    assert response.content == b"webp-bytes"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"] == '"abc"'
    assert seen["url"].startswith(settings.storage.imgproxy.url)

    async def unreachable(url):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(renderer, "fetch", unreachable)
    down = await client.get(thumb_url(media.id), headers=headers(user))
    assert down.status_code == 503


@pytest.mark.asyncio
async def test_thumbnail_respects_post_scope(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    _, user, observation = await _scene(db)
    media = await make_media(db, observation, user)
    other_post = await make_post(db)
    outsider = await make_user(db, perms=VIEW, post_id=other_post.id)
    denied = await client.get(thumb_url(media.id), headers=headers(outsider))
    assert denied.status_code == 403


# ----------------------------------------------------------------- videos and audio

@pytest.mark.asyncio
async def test_video_states(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    _, user, observation = await _scene(db)
    pending = await make_media(db, observation, user, media_type="video")
    failed = await make_media(db, observation, user, media_type="video", poster_status="failed", poster_attempts=3)
    ready = await make_media(db, observation, user, media_type="video", poster_status="ready")
    audio = await make_media(db, observation, user, media_type="audio")

    response = await client.get(thumb_url(pending.id), headers=headers(user))
    assert response.status_code == 202
    assert response.headers["retry-after"] == "2"
    assert response.headers["x-thumbnail-status"] == "pending"
    assert response.content == b""
    await db.refresh(pending)
    assert pending.poster_status == "pending" and pending.poster_attempts == 0, "a poll must not touch the job"

    response = await client.get(thumb_url(failed.id), headers=headers(user))
    assert response.status_code == 409
    assert response.json()["attempts"] == 3

    response = await client.get(thumb_url(ready.id), headers=headers(user))
    assert response.status_code == 200
    assert decode_source(response.headers["x-accel-redirect"]) == f"local:///_derived/posters/000/{ready.id}.jpg"

    response = await client.get(thumb_url(audio.id), headers=headers(user))
    assert response.status_code == 422


# ----------------------------------------------------------------- list responses

@pytest.mark.asyncio
async def test_media_info_carries_status_and_signed_urls(client, db, headers):
    _, user, observation = await _scene(db)
    image = await make_media(db, observation, user)
    video = await make_media(db, observation, user, media_type="video")

    info = (await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=image.id), headers=headers(user))).json()
    assert info["thumbnail_status"] == "ready"
    assert set(info["thumbnail_urls"]) == {"small", "medium", "large", "preview"}
    assert info["thumbnail_urls"]["medium"].startswith(settings.storage.imgproxy.public_prefix + "/")
    assert "/exp:" in info["thumbnail_urls"]["medium"]
    assert "thumbnail_id" not in info

    info = (await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=video.id), headers=headers(user))).json()
    assert info["thumbnail_status"] == "pending" and info["thumbnail_urls"] is None

    listing = await client.get(V1.path(MediaRoutes.ROOT), params={"has_thumbnail": "true"}, headers=headers(user))
    assert [m["id"] for m in listing.json()] == [image.id]
    listing = await client.get(V1.path(MediaRoutes.ROOT), params={"has_thumbnail": "false"}, headers=headers(user))
    assert [m["id"] for m in listing.json()] == [video.id]


# ----------------------------------------------------------------- share

@pytest.mark.asyncio
async def test_share_link(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage.thumbnail, "share_max_ttl_seconds", 7200)
    _, user, observation = await _scene(db)
    image = await make_media(db, observation, user)
    video = await make_media(db, observation, user, media_type="video")
    url = V1.path(MediaRoutes.MEDIA_SHARE)

    response = await client.post(url.format(media_id=image.id), json={"size": "large", "ttl_seconds": 999999}, headers=headers(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["size"] == "large" and "/rs:fit:600:600:0:0/" in body["url"] and "/exp:" in body["url"]
    assert body["url"].startswith(settings.storage.imgproxy.public_prefix + "/")
    assert body["expires_at"].startswith("20")

    pending = await client.post(url.format(media_id=video.id), json={}, headers=headers(user))
    assert pending.status_code == 202

    short = await client.post(url.format(media_id=image.id), json={"ttl_seconds": 5}, headers=headers(user))
    assert short.status_code == 422


# ----------------------------------------------------------------- regenerate and administration

@pytest.mark.asyncio
async def test_regenerate_and_poster_administration(client, db, headers, storage):
    post, user, observation = await _scene(db)
    admin = await make_admin(db, post_id=post.id)
    image = await make_media(db, observation, user)
    video = await make_media(db, observation, user, media_type="video", poster_status="ready", poster_attempts=2, poster_worker="w")
    poster = video.resolve_poster_path(storage)
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpeg")

    regen = V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE)
    assert (await client.post(regen.format(media_id=video.id), headers=headers(user))).status_code == 403

    response = await client.post(regen.format(media_id=video.id), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["thumbnail_status"] == "pending"
    assert not poster.exists()
    await db.refresh(video)
    assert video.poster_attempts == 0 and video.poster_worker is None

    response = await client.post(regen.format(media_id=image.id), headers=headers(admin))
    assert response.status_code == 200 and response.json()["thumbnail_status"] == "ready"

    status_url = V1.path(MediaRoutes.POSTERS_STATUS)
    assert (await client.get(status_url, headers=headers(user))).status_code == 403
    status = (await client.get(status_url, headers=headers(admin))).json()
    assert status["posters"]["pending"] == 1 and status["workers"] == [] and status["worker_alive"] is False

    # a ready video whose poster file is missing gets re-queued by reconcile
    missing = await make_media(db, observation, user, media_type="video", poster_status="ready")
    reconcile_url = V1.path(MediaRoutes.POSTERS_RECONCILE)
    assert (await client.post(reconcile_url, json={}, headers=headers(user))).status_code == 403
    response = await client.post(reconcile_url, json={"retry_failed": False}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json() == {"failed_abandoned": 0, "queued_untracked": 0, "requeued_missing": 1, "retried_failed": 0}
    await db.refresh(missing)
    assert missing.poster_status == "pending"
