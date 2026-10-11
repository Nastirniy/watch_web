"""Media URLs and the checks of the media routes: the poster cache buster, the share link expiry, Range without
nginx (RFC 9110) and the source check before a thumbnail is handed to nginx."""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.models.media import MediaFile
from app.backend.services.media.urls import processing_path_for, share_expiry
from tests.helpers import make_dictionary, make_media, make_observation, make_post, make_user

ORIGINAL_ROUTES = (MediaRoutes.MEDIA_DOWNLOAD, MediaRoutes.MEDIA_STREAM)
CONTENT = b"0123456789"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    return tmp_path


async def _scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view"], post_id=post.id)
    return user, await make_observation(db, user, post, ids["signal_type_id"])


def _video(**fields) -> MediaFile:
    values = dict(id=5, media_type="video", file_path="2026/09/02/obs_1/v.mp4", poster_status="ready")
    values.update(fields)
    return MediaFile(**values)


# ---------------------------------------------------------------- poster cache buster

def test_a_poster_path_carries_the_cache_buster_of_its_generation():
    first = processing_path_for(_video(poster_generated_at=datetime(2026, 9, 4, 12, 0, 0)))
    again = processing_path_for(_video(poster_generated_at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)))
    regenerated = processing_path_for(_video(poster_generated_at=datetime(2026, 9, 4, 12, 5, 0)))
    stamp = int(datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    assert f"/cb:{stamp}/" in first
    assert first == again, "naive means UTC"
    assert first != regenerated, "a regenerated poster gets another URL, so another nginx cache key"


def test_image_paths_and_posters_without_a_generation_time_stay_as_they_were():
    assert "cb:" not in processing_path_for(_video(poster_generated_at=None))
    image = MediaFile(id=6, media_type="image", file_path="a/b.jpg", poster_generated_at=datetime(2026, 9, 4))
    assert "cb:" not in processing_path_for(image)


# ---------------------------------------------------------------- share expiry

@pytest.mark.parametrize("ttl", [60, 600, 3600, 7 * 24 * 3600])
def test_a_share_link_never_outlives_its_ttl_and_is_stable_inside_a_window(ttl):
    window = max(60, ttl // 10)
    start = datetime(2026, 9, 4, 12, 17, 33, tzinfo=timezone.utc)
    seen = set()
    for step in range(0, 3 * window, 7):
        now = start + timedelta(seconds=step)
        expires = share_expiry(ttl, now)
        assert now + timedelta(seconds=ttl - window) < expires <= now + timedelta(seconds=ttl)
        seen.add(expires)
    assert len(seen) <= 4, "the expiry moves once per window, not per request"
    assert share_expiry(ttl, start) == share_expiry(ttl, start + timedelta(seconds=1)) or window < 2


@pytest.mark.asyncio
async def test_the_share_route_caps_the_expiry_at_the_maximum_ttl(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage.thumbnail, "share_max_ttl_seconds", 3600)
    user, observation = await _scene(db)
    image = await make_media(db, observation, user)
    before = datetime.now(timezone.utc)
    response = await client.post(V1.path(MediaRoutes.MEDIA_SHARE, media_id=image.id),
                                 json={"ttl_seconds": 999999}, headers=headers(user))
    assert response.status_code == 200, response.text
    expires = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00"))
    assert expires <= before + timedelta(seconds=3600) + timedelta(seconds=5)
    assert expires > before + timedelta(seconds=3600 - 360 - 5)


# ---------------------------------------------------------------- Range without nginx

async def _original(db, storage, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    user, observation = await _scene(db)
    media = await make_media(db, observation, user)
    path = storage / media.file_path
    path.parent.mkdir(parents=True)
    path.write_bytes(CONTENT)
    return user, media


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
@pytest.mark.parametrize("value,expected,content_range", [
    ("bytes=0-3", b"0123", "bytes 0-3/10"),
    ("bytes=4-", b"456789", "bytes 4-9/10"),
    ("bytes=-3", b"789", "bytes 7-9/10"),
    ("bytes=-100", CONTENT, "bytes 0-9/10"),
    ("bytes=8-100", b"89", "bytes 8-9/10"),
])
async def test_a_single_byte_range_is_served(client, db, headers, storage, monkeypatch, route, value, expected,
                                             content_range):
    user, media = await _original(db, storage, monkeypatch)
    response = await client.get(V1.path(route, media_id=media.id), headers={**headers(user), "Range": value})
    assert response.status_code == 206, response.text
    assert response.content == expected
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["bytes=abc", "bytes=5-2", "bytes=-", "bytes=0-1,3-4", "items=0-1", "bytes=", "garbage"])
async def test_an_invalid_or_unsupported_range_is_ignored(client, db, headers, storage, monkeypatch, value):
    user, media = await _original(db, storage, monkeypatch)
    response = await client.get(V1.path(MediaRoutes.MEDIA_STREAM, media_id=media.id),
                                headers={**headers(user), "Range": value})
    assert response.status_code == 200, response.text
    assert response.content == CONTENT
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
@pytest.mark.parametrize("value", ["bytes=10-", "bytes=10-20", "bytes=-0", "bytes=99-100"])
async def test_an_unsatisfiable_range_is_a_416_problem(client, db, headers, storage, monkeypatch, route, value):
    user, media = await _original(db, storage, monkeypatch)
    response = await client.get(V1.path(route, media_id=media.id), headers={**headers(user), "Range": value})
    assert response.status_code == 416
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["content-range"] == "bytes */10"
    assert response.json()["code"] == "range_not_satisfiable"


@pytest.mark.asyncio
async def test_no_range_header_serves_the_whole_file(client, db, headers, storage, monkeypatch):
    user, media = await _original(db, storage, monkeypatch)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id), headers=headers(user))
    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-length"] == "10"
    assert response.headers["content-disposition"].startswith("attachment;")


# ---------------------------------------------------------------- thumbnail source check

@pytest.mark.asyncio
async def test_a_thumbnail_whose_image_is_missing_is_media_file_missing(client, db, headers, storage, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    user, observation = await _scene(db)
    image = await make_media(db, observation, user)
    url = V1.path(MediaRoutes.MEDIA_THUMBNAIL, media_id=image.id)
    missing = await client.get(url, headers=headers(user))
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "media_file_missing"

    (storage / image.file_path).parent.mkdir(parents=True)
    (storage / image.file_path).write_bytes(b"jpeg")
    assert (await client.get(url, headers=headers(user))).status_code == 200


@pytest.mark.asyncio
async def test_a_thumbnail_whose_poster_is_missing_is_media_file_missing(client, db, headers, storage, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    user, observation = await _scene(db)
    video = await make_media(db, observation, user, media_type="video", poster_status="ready")
    url = V1.path(MediaRoutes.MEDIA_THUMBNAIL, media_id=video.id)
    assert (await client.get(url, headers=headers(user))).json()["code"] == "media_file_missing"

    poster = video.resolve_poster_path(storage)
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpeg")
    assert (await client.get(url, headers=headers(user))).status_code == 200
